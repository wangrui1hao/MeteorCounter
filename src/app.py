"""MeteorCounter: a local desktop-pixel event counter for 洛克王国：世界."""
import os
import sys
import time
import queue
import ctypes
from ctypes import wintypes
import threading
import traceback
import uuid
import copy
import json
import sqlite3
import shutil
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Set per-monitor physical pixels before creating Tk or MSS objects.
from capture import find_game, region_visible, capture_region, monitor_work_area
from detector import EventDetector, EventTracker
import winsound
from storage import Store
from inventory import InventoryDetector, BagStabilizer, InventorySession
from inventory_view import InventoryView
from timing import MeteorTiming, duration
from diagnostics import EvidenceRecorder, BagObservationGate, EvidenceError, VERSION
from maintenance import user_directory, import_legacy_data, clear_history, clear_old_cache
import cv2
import mss
from PIL import Image
import tkinter as tk
from tkinter import ttk, messagebox

APP_DIR = user_directory()
BG = '#101827'
CARD = '#1b283c'
FG = '#edf3ff'
MUTED = '#a2b3cb'
GREEN = '#8ee3c1'
BLUE = '#79adff'
GOLD = '#f5d184'
STRICT_THRESHOLD = .80
METEOR_PERIOD = .10
BAG_PERIOD = .42


def play_shower_alert():
    path=Path(__file__).parent/'assets'/'shower_alert.wav'
    if not path.exists():raise OSError('流星雨提示音文件不存在')
    winsound.PlaySound(str(path),winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


class CounterApp:
    def __init__(self, resume_round=None, start_paused=False):
        self.root = tk.Tk()
        self.root.withdraw()  # Do not expose Tk's default icon or partial layout.
        self.root.title(f'陨星计数器 · {VERSION}')
        self.root.configure(bg=BG)
        self.root.resizable(True, True)
        icon=Path(__file__).parent/'assets'/'app.ico'
        if icon.exists():
            self.root.iconbitmap(str(icon))
        self.store = Store(APP_DIR / 'data')
        self.ball_prices = self.store.prices()
        self.evidence = EvidenceRecorder(self.store.directory)
        self.recent_records = deque(maxlen=100)
        _, self.persisted_active = self.store.state()
        _,self.persisted_shower_active=self.store.shower_state()
        self.messages = queue.Queue()
        self.round_lock = threading.Lock()
        self.stop = threading.Event()
        self.diagnostic_refresh = threading.Event()
        self.running = threading.Event()
        if not start_paused:self.running.set()
        self.threshold = STRICT_THRESHOLD
        self.meteor_timing = MeteorTiming()
        self.failed = False
        self.closing = False
        self.inventory=InventorySession()
        # Reopening the tool continues the saved round. Only the explicit
        # "new round" button creates another one after the first launch.
        resume_round=resume_round or self.store.latest_round_id()
        self.session_id=resume_round or uuid.uuid4().hex
        if resume_round:
            self.session_meteors,rows,history=self.store.load_latest_round(resume_round)
            self.inventory.rows=rows;self.inventory.initialized=bool(rows)
            self.inventory.last_signature=tuple(sorted((k,r['current']) for k,r in rows.items())) if rows else None
            now=time.monotonic();wall=datetime.now().astimezone().timestamp()
            saved_timing=self.store.load_timing(self.session_id,self.session_meteors)
            if saved_timing:
                # No pixels are monitored while the program is closed. Resume
                # from the saved active-time ages, without adding offline time.
                self.meteor_timing.restore(saved_timing,now,elapsed=0.)
                start_paused=start_paused or self.meteor_timing.paused
            else:
                for when,evidence in history:
                    # Old versions saved capture time in the evidence manifest, before counting.
                    stamp=datetime.fromisoformat(when).timestamp()
                    if evidence and Path(evidence).suffix=='.json':
                        try:
                            record=json.loads(Path(evidence).read_text(encoding='utf-8'))
                            shot=next(im for im in record['images'] if im['file']=='trigger.png')
                            stamp=datetime.fromisoformat(shot['time']).timestamp()
                        except (OSError,ValueError,KeyError,StopIteration):pass
                    self.meteor_timing.add(now-(wall-stamp))
            self.evidence.record('round_resumed',self.session_id,meteors=self.session_meteors)
            self.recent_records.appendleft(f'{datetime.now():%H:%M:%S}  已继续原有轮次')
        else:
            self.store.start_round(self.session_id)
            self.evidence.start(self.session_id)
            self.recent_records.appendleft(f'{datetime.now():%H:%M:%S}  开始新一轮')
            self.session_meteors=0
        self.bag_epoch=0
        self.session_showers=self.store.session_showers(self.session_id)
        self.round_started=self.store.round_started(self.session_id)
        if start_paused:
            self.running.clear()
            self.meteor_timing.pause(time.monotonic())
        self.save_timing()
        self._build_ui()
        if start_paused:
            self.pause_button.config(text='继续计数')
            self.status_label.config(text='已暂停，累计记录已保留',fg=MUTED)
        if resume_round:
            for key,row in self.inventory.rows.items():
                if key in self.bag_view.icons:self.bag_view.set_amounts(key,row['initial'],row['used'])
            self.update_totals();self.update_timing()
            if history:
                self.last_label.config(text=f'最近一次  {datetime.fromisoformat(history[-1][0]).astimezone():%H:%M:%S}')
            rare=self.store.last_shower(self.session_id)
            if rare:self.shower_last_label.config(text=f'最近一次  {datetime.fromisoformat(rare).astimezone():%H:%M:%S}')
        self.root.update_idletasks()
        wanted_width=max(1320,self.root.winfo_reqwidth()+20)
        wanted_height=max(640,self.root.winfo_reqheight()+20)
        self._startup_size=(wanted_width,wanted_height)
        self.root.geometry(f'{wanted_width}x{wanted_height}')
        # Determine the minimum once; status updates must not resize the window.
        self.root.minsize(max(1020,self.main.winfo_reqwidth()),
                          max(640,self.main.winfo_reqheight()+12))
        self._place_on_free_monitor()
        self._refresh_history()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.worker = threading.Thread(target=self.monitor_loop, daemon=True)
        self.worker.start()
        self.bag_worker=threading.Thread(target=self.inventory_loop,daemon=True)
        self.bag_worker.start()
        self.root.after(70, self.poll)
        self.root.after(1000, self.tick_timing)

    def label(self, parent, text='', size=11, color=FG, bold=False, **kw):
        return tk.Label(parent, text=text, font=('Microsoft YaHei UI', size, 'bold' if bold else 'normal'),
                        bg=parent['bg'], fg=color, **kw)

    def _build_ui(self):
        style=ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('TButton', font=('Microsoft YaHei UI',11), padding=(10,8),
                        foreground=FG, background='#304764', borderwidth=0)
        style.map('TButton', background=[('active','#43658c')])
        style.configure('Primary.TButton', foreground=BG, background=GREEN)
        style.map('Primary.TButton', background=[('active','#b5f5dd')])
        style.layout('History.Vertical.TScrollbar', [
            ('Vertical.Scrollbar.trough', {'sticky':'ns', 'children':[
                ('Vertical.Scrollbar.thumb', {'expand':'1', 'sticky':'nswe'})]})])
        style.configure('History.Vertical.TScrollbar', background='#40536d',
            troughcolor=CARD, bordercolor=CARD, lightcolor='#40536d',
            darkcolor='#40536d', borderwidth=0, arrowsize=8, gripcount=0)
        style.map('History.Vertical.TScrollbar',
            background=[('pressed','#819bbd'),('active','#607b9d')],
            lightcolor=[('pressed','#819bbd'),('active','#607b9d')],
            darkcolor=[('pressed','#819bbd'),('active','#607b9d')])
        main=tk.Frame(self.root,bg=BG,padx=24,pady=20);main.pack(fill='both',expand=True)
        self.main=main
        main.columnconfigure(0,weight=1)
        main.rowconfigure(2,weight=1)
        head=tk.Frame(main,bg=BG);head.grid(row=0,column=0,sticky='ew')
        brand=tk.Frame(head,bg=BG);brand.pack(side='left')
        self.label(brand,'✦  陨星计数器',21,bold=True).pack(anchor='w')
        controls=tk.Frame(head,bg=BG);controls.pack(side='right')
        self.controls=controls
        self.save_images=tk.BooleanVar(value=False)
        tk.Checkbutton(controls,text='保存诊断截图',variable=self.save_images,command=self.toggle_diagnostics,
                       bg=BG,fg=MUTED,selectcolor=CARD,activebackground=BG,activeforeground=FG,
                       font=('Microsoft YaHei UI',10)).pack(side='left',padx=(0,12))
        self.topmost=tk.BooleanVar(value=False)
        tk.Checkbutton(controls,text='置顶',variable=self.topmost,command=self.apply_topmost,
                       bg=BG,fg=MUTED,selectcolor=CARD,activebackground=BG,activeforeground=FG,
                       font=('Microsoft YaHei UI',10)).pack(side='left',padx=(0,12))
        ttk.Button(controls,text='开始新一轮',style='Primary.TButton',command=self.new_round).pack(side='left')
        self.pause_button=ttk.Button(controls,text='暂停计数',command=self.toggle)
        self.pause_button.pack(side='left',padx=(10,0))
        for text,command in [('打开目录',lambda:self.open_directory(APP_DIR)),('清理缓存',self.clean_cache)]:
            ttk.Button(controls,text=text,command=command).pack(side='left',padx=(10,0))

        metrics=tk.Frame(main,bg=BG);metrics.grid(row=1,column=0,sticky='ew',pady=(16,16))
        metrics.columnconfigure((0,1,2,3),weight=1,uniform='metrics')
        cards=[]
        for i in range(4):
            card=tk.Frame(metrics,bg=CARD,padx=18,pady=12)
            card.grid(row=0,column=i,sticky='nsew',padx=(0,12) if i<3 else (0,0))
            cards.append(card)
        self.label(cards[0],'本轮陨星',11,color=MUTED).pack(anchor='w')
        self.count_label=self.label(cards[0],'0',32,color=GREEN,bold=True);self.count_label.pack(anchor='w',pady=(6,4))
        self.start_label=self.label(cards[0],self.round_start_text(),9,color=MUTED)
        self.start_label.pack(anchor='w',pady=(0,3))
        self.last_label=self.label(cards[0],'本轮尚未出现陨星',9,color=MUTED);self.last_label.pack(anchor='w')
        self.label(cards[1],'本轮流星雨',11,color=GOLD).pack(anchor='w')
        self.shower_label=self.label(cards[1],'0',32,color=GOLD,bold=True);self.shower_label.pack(anchor='w',pady=(6,4))
        self.shower_last_label=self.label(cards[1],'本轮尚未出现流星雨',9,color=MUTED);self.shower_last_label.pack(anchor='w',pady=(0,3))
        self.alert_hint=self.label(cards[1],'触发时播放提示音',9,color=MUTED);self.alert_hint.pack(anchor='w')
        self.label(cards[2],'最近 5 次 · 平均间隔',11,color=MUTED).pack(anchor='w')
        self.average_label=self.label(cards[2],'—',28,color=FG,bold=True);self.average_label.pack(anchor='w',pady=(6,4))
        self.average_hint=self.label(cards[2],'出现 2 次后开始计算',9,color=MUTED);self.average_hint.pack(anchor='w')
        self.label(cards[3],'距上次陨星',11,color=MUTED).pack(anchor='w')
        self.since_label=self.label(cards[3],'—',28,color=BLUE,bold=True);self.since_label.pack(anchor='w',pady=(6,4))
        self.since_hint=self.label(cards[3],'手动暂停时停止计时',9,color=MUTED)
        self.since_hint.pack(anchor='w')

        body=tk.Frame(main,bg=BG);body.grid(row=2,column=0,sticky='nsew')
        left=tk.Frame(body,bg=CARD,padx=18,pady=16);left.pack(side='left',fill='y',padx=(0,18))
        right=tk.Frame(body,bg=BG);right.pack(side='left',fill='both',expand=True)
        self.label(left,'监测状态',13,bold=True).pack(anchor='w')
        self.label(left,'严格识别 · 已固定',9,color=GREEN).pack(anchor='w',pady=(5,12))
        self.status_label=self.label(left,'正在寻找游戏窗口…',10,color=FG,anchor='nw',justify='left',wraplength=290,height=2)
        self.status_label.pack(fill='x',pady=(0,8))
        self.screen_label=self.label(left,'',9,color=MUTED,anchor='nw',justify='left',wraplength=290,height=2)
        self.screen_label.pack(fill='x',pady=(0,8))
        self.score_label=self.label(left,'匹配分数 —',9,color=MUTED,anchor='nw',justify='left',wraplength=290,height=2)
        self.score_label.pack(fill='x')
        self.diagnostic_usage=self.label(left,'诊断文件占用检查中…',9,color=MUTED,wraplength=290,justify='left',anchor='nw',height=2)
        self.diagnostic_usage.pack(fill='x',pady=(8,0))
        tk.Frame(left,bg='#304159',height=1).pack(fill='x',pady=18)
        self.label(left,'最近间隔',11,bold=True).pack(anchor='w')
        intervals=tk.Frame(left,bg=CARD);intervals.pack(fill='x',pady=(7,0))
        intervals.columnconfigure(1,weight=1)
        self.interval_values=[]
        for index,title in enumerate(('最近一次','往前第 1 次','往前第 2 次','往前第 3 次')):
            self.label(intervals,title,9,color=MUTED).grid(row=index,column=0,sticky='w',pady=3,padx=(0,12))
            value=self.label(intervals,'—',10,color=GREEN if index==0 else BLUE)
            value.grid(row=index,column=1,sticky='e',pady=3)
            self.interval_values.append(value)
        self.label(left,'最近记录',11,bold=True).pack(anchor='w',pady=(18,7))
        history_frame=tk.Frame(left,bg=CARD)
        history_frame.pack(fill='both',expand=True)
        history_frame.columnconfigure(0,weight=1)
        history_frame.columnconfigure(1,minsize=14)
        history_frame.rowconfigure(0,weight=1)
        self.history=tk.Text(history_frame,height=3,width=1,wrap='word',
            font=('Microsoft YaHei UI',9),bg=CARD,fg=MUTED,bd=0,
            highlightthickness=0,state='disabled')
        history_scroll=ttk.Scrollbar(history_frame,orient='vertical',
            style='History.Vertical.TScrollbar',command=self.history.yview)
        history_scroll.grid(row=0,column=1,sticky='ns',padx=(6,0))
        def update_history_scroll(first,last):
            history_scroll.set(first,last)
            if float(first)<=0 and float(last)>=1:
                history_scroll.grid_remove()
            else:
                history_scroll.grid()
        self.history.config(yscrollcommand=update_history_scroll)
        self.history.grid(row=0,column=0,sticky='nsew')
        self._build_bag_ui(style,right)

    def toggle_diagnostics(self):
        enabled=self.save_images.get()
        if enabled and not messagebox.askyesno('保存诊断截图',
                '为定位识别问题，将在本机保存游戏区域截图。\n'
                '画面可能包含游戏昵称或覆盖在游戏上的通知、其他窗口。\n'
                '截图不上传，保留 3～7 天，最多保留 500 MiB。\n\n'
                '写入中的截图可能短暂超限，完成后自动清理。\n\n仅本次运行开启，下次启动默认关闭。是否开启？',parent=self.root):
            enabled=False;self.save_images.set(False)
        self.evidence.set_save_images(enabled)
        self.note('诊断截图已开启' if enabled else '诊断截图已关闭；已有截图按原规则保留')

    def open_directory(self,path):
        path.mkdir(parents=True,exist_ok=True)
        os.startfile(path)

    def clean_cache(self):
        if self.closing:return
        if not messagebox.askyesno('清理缓存',
                '删除已结束轮次及其已完成截图、今天之前的日志和超过一天的闲置解压缓存？\n\n'
                '保留当前轮次、当前截图、球图标、单价和正在使用的程序文件。此操作不能撤销。',parent=self.root):return
        try:
            with self.evidence.lock:
                result=clear_history(self.store,self.session_id)
                removed=clear_old_cache(APP_DIR,getattr(sys,'_MEIPASS',None))
        except (OSError,ValueError,KeyError,sqlite3.Error) as error:
            messagebox.showerror('清理未完成',f'部分项目可能已清理，请检查后重试。\n{error}',parent=self.root)
            return
        finally:
            self.diagnostic_refresh.set()
        self.note(f"已清理 {result['rounds']} 个历史轮次、{result['evidence']} 组截图、{removed} 项日志和缓存")
        if result['incomplete']:
            self.note(f"保留 {result['incomplete']} 组尚未完成的截图")

    def _build_bag_ui(self, style,panel):
        panel.columnconfigure(0,weight=1)
        panel.rowconfigure(2,weight=1)
        title=tk.Frame(panel,bg=BG);title.grid(row=0,column=0,sticky='ew')
        self.label(title,'咕噜球背包',16,bold=True).pack(side='left')
        self.bag_cost=self.label(title,'本轮成本  0',12,color=GOLD,bold=True)
        self.bag_cost.pack(side='right')
        self.bag_status=self.label(panel,'首次开包记录初始数量，之后开包核对消耗',10,color=MUTED,wraplength=610,justify='left')
        self.bag_status.grid(row=1,column=0,sticky='w',pady=(6,14))
        self.bag_view=InventoryView(panel,BG,CARD,FG,MUTED,GREEN,self.edit_ball_price)
        self.bag_view.grid(row=2,column=0,sticky='nsew')
        custom=self.store.directory/'ball_icons'
        custom.mkdir(parents=True,exist_ok=True)
        for path in sorted((Path(__file__).parent/'assets'/'bag').glob('ball_*.png')):
            saved=custom/path.name
            if not saved.exists():shutil.copyfile(path,saved)
        for path in sorted(custom.glob('ball_*.png')):
            with Image.open(path) as icon:self.add_ball_row(path.stem,icon.copy())
        if custom.exists():
            aliases=InventoryDetector(custom).aliases
            for path in sorted(custom.glob('custom_*.png')):
                if path.stem not in aliases:self.add_ball_row(path.stem,Image.open(path).copy())

    def round_start_text(self):
        if not self.round_started:return '本轮开始  等待首颗陨星'
        when=datetime.fromisoformat(self.round_started).astimezone()
        return f'本轮开始  {when:%m-%d %H:%M:%S}'

    def add_ball_row(self, key, icon):
        self.bag_view.add(key,icon)
        if key in self.ball_prices:self.bag_view.set_price(key,self.ball_prices[key])

    def edit_ball_price(self, key, price):
        if self.closing:return False
        try:self.store.set_price(key,price)
        except (ValueError,sqlite3.Error) as error:
            messagebox.showerror('单价未保存',str(error),parent=self.root);return False
        if price==0:self.ball_prices.pop(key,None)
        else:self.ball_prices[key]=price
        self.bag_view.set_price(key,price)
        self.update_totals()
        return True

    def new_round(self):
        if self.closing:return
        if not messagebox.askyesno('开始新一轮','同时清零陨星、流星雨和全部用球统计？下次开包重新建立初值，首颗陨星确定开始时间，历史记录保留。',parent=self.root):return
        with self.round_lock:
            self.session_id=uuid.uuid4().hex;self.store.start_round(self.session_id)
            self.bag_epoch+=1;self.inventory=InventorySession();self.session_meteors=0
            self.session_showers=0
        self.round_started=self.store.round_started(self.session_id)
        self.start_label.config(text=self.round_start_text())
        self.evidence.start(self.session_id)
        self.note('开始新一轮')
        self.bag_view.reset()
        self.meteor_timing.reset()
        self.save_timing()
        self.update_timing()
        self.update_totals()
        self.last_label.config(text='本轮尚未出现陨星')
        self.shower_last_label.config(text='本轮尚未出现流星雨')
        self._refresh_history()
        self.bag_status.config(text='等待首次打开背包，建立新的初始值',fg=MUTED)

    def round_context(self):
        with self.round_lock:
            return self.bag_epoch,self.session_id

    def update_totals(self):
        self.count_label.config(text=str(self.session_meteors))
        self.shower_label.config(text=str(self.session_showers))
        cost=sum(r['used']*self.ball_prices.get(key,0) for key,r in self.inventory.rows.items())
        self.bag_cost.config(text=f'本轮成本  {cost:,}')

    def update_timing(self):
        timing=self.meteor_timing
        self.average_label.config(text=duration(timing.average))
        count=len(timing.events)
        self.average_hint.config(text=(f'{count} 次陨星 · {count-1} 段间隔'+(' · 样本积累中' if count<5 else '')
                                       if count>=2 else '出现 2 次后开始计算'))
        self.since_label.config(text=duration(timing.since_last(time.monotonic())))
        self.since_hint.config(text='已暂停，计时已冻结' if timing.paused else '手动暂停时停止计时')
        gaps=list(reversed(timing.intervals))
        for index,label in enumerate(self.interval_values):
            label.config(text=duration(gaps[index]) if index<len(gaps) else '—')

    def tick_timing(self):
        if not self.stop.is_set():
            self.update_timing()
            self.root.after(1000,self.tick_timing)

    def inventory_loop(self):
        try:detector=InventoryDetector(self.store.directory/'ball_icons')
        except Exception as error:
            self.post(bag_status=f'识别资源加载失败：{str(error)[:80]}');return
        stable=BagStabilizer();last_keys={k for k,r in self.inventory.rows.items() if r['current']>0};observations=BagObservationGate()
        if self.inventory.initialized:
            observations.signature=tuple(sorted((k,r['current']) for k,r in self.inventory.rows.items() if r['current']>0))
        last_maintenance=float('-inf');bag_visible=False
        sct=None;last_refresh=0;identity=None;last_epoch=self.bag_epoch
        capture_state=None;last_probe=float('-inf');last_health=float('-inf');last_unconfirmed=float('-inf')
        last_unconfirmed_seen=float('-inf')
        try:
            while not self.stop.is_set():
                begin=time.monotonic()
                if self.diagnostic_refresh.is_set() or begin-last_maintenance>=60:
                    self.diagnostic_refresh.clear()
                    try:self.post(diagnostic_usage=self.evidence.maintain())
                    except (OSError,ValueError,KeyError) as error:
                        self.post(diagnostic_error=f'诊断清理未完成：{str(error)[:80]}')
                    last_maintenance=begin
                if not self.running.is_set():stable.reset();self.stop.wait(.3);continue
                try:
                    epoch,session=self.round_context()
                    if sct is None or begin-last_refresh>5:
                        if sct:sct.close()
                        sct=mss.MSS();last_refresh=begin
                    target,_=find_game(sct.monitors)
                    if not target:
                        if capture_state!='waiting':
                            self.evidence.record('bag_capture_state',session,state='waiting_for_game',monitors=sct.monitors)
                            capture_state='waiting'
                        stable.reset();self.post(bag_status='等待游戏窗口');self.stop.wait(.6);continue
                    current=(target.hwnd,target.left,target.top,target.width,target.height)
                    if current!=identity or last_epoch!=epoch:
                        stable.reset();identity=current
                        if last_epoch!=epoch:
                            last_keys=set();observations.reset();bag_visible=False
                        last_epoch=epoch
                        capture_state=None;last_probe=last_health=last_unconfirmed=float('-inf')
                        last_unconfirmed_seen=float('-inf')
                        self.evidence.record('bag_capture_context',session,target=asdict(target),monitors=sct.monitors,
                                             region=detector.region(target.width,target.height))
                    region=detector.region(target.width,target.height)
                    if not region_visible(target,region,sct.monitors[0]):
                        if capture_state!='blocked':
                            self.evidence.record('bag_capture_state',session,state='occluded_or_outside_screen',target=asdict(target),region=region)
                            capture_state='blocked'
                        stable.reset();self.post(bag_status='背包区域被遮挡或超出屏幕；请调整窗口位置，确保背包完整可见');self.stop.wait(.5);continue
                    frame=capture_region(sct,target,region)
                    refreshed,_=find_game(sct.monitors)
                    if refreshed!=target or not region_visible(target,region,sct.monitors[0]):
                        stable.reset();self.stop.wait(.4);continue
                    if capture_state!='visible':
                        self.evidence.record('bag_capture_state',session,state='visible',target=asdict(target),region=region)
                        capture_state='visible'
                    probe=begin-last_probe>=5
                    if probe:last_probe=begin
                    result=detector.detect(frame,target.height,probe=probe)
                    accepted=stable.push(result,time.monotonic())
                    details=dict(target=asdict(target),region=region,header_score=result.header_score,
                        valid=result.valid,compact=result.compact,reason=result.reason,layout=result.layout,stable_frames=stable.frames,
                        balls=[dict(key=b.key,amount=b.amount,slot=b.slot,digit_score=b.quality,icon_score=b.icon_quality) for b in result.balls])
                    # Retain native pixels so replay uses the same scale search
                    # and interpolation, without a second lossy normalization.
                    def save_reading(kind):
                        return self.evidence.save(kind,session,[('inventory',frame,dict(full_height=target.height))],**details)
                    if accepted:
                        observed={b.key for b in accepted.balls if b.key}
                        if any(b.key is None for b in accepted.balls) and last_keys-observed:
                            details['reason']='球图标可能被鼠标遮挡，请把鼠标移到球格外再核对'
                            details['valid']=False
                            accepted=None
                        else:
                            for ball in accepted.balls:detector.register(ball)
                            for entry,ball in zip(details['balls'],accepted.balls):entry['key']=ball.key
                            last_keys={b.key for b in accepted.balls}
                            snapshot={b.key:b.amount for b in accepted.balls}
                            if observations.changed(snapshot):
                                observations.evidence=save_reading('bag_snapshot')
                                self.post(bag_snapshot=snapshot,
                                    bag_icons={b.key:Image.fromarray(cv2.cvtColor(b.icon,cv2.COLOR_BGR2RGB)) for b in accepted.balls},
                                    bag_epoch=epoch,bag_compact=accepted.compact,evidence=observations.evidence)
                            else:self.post(bag_status=f'已核对 {len(snapshot)} 种球，数量未变',bag_epoch=epoch,bag_ok=True)
                    if result.visible:
                        bag_visible=True
                        if not details['valid'] and observations.rejection_due(details['reason'],begin):
                            rejected=save_reading('bag_rejected')
                            observations.evidence=rejected
                            self.post(bag_note='背包未更新'+(' · 已保存截图' if rejected else ''),bag_epoch=epoch)
                        else:rejected=None
                        sample=(accepted is not None,details['reason'],tuple((b.key,b.amount) for b in result.balls))
                        if observations.sample_due(sample,begin):
                            self.evidence.record('bag_sample',session,accepted=accepted is not None,
                                evidence=rejected or observations.evidence,**details)
                        if accepted is None:self.post(bag_status=details['reason'] or '正在确认图标和数字，请保持背包打开约 1 秒',bag_epoch=epoch)
                    else:
                        if bag_visible:self.evidence.record('bag_closed',session)
                        bag_visible=False
                        if begin-last_health>=15:
                            self.evidence.record('bag_search',session,**details);last_health=begin
                        if result.layout and result.layout.get('diagnostic_only'):
                            last_unconfirmed_seen=begin
                            if begin-last_unconfirmed>=60:
                                save_reading('bag_unconfirmed');last_unconfirmed=begin
                            self.post(bag_status=result.reason,bag_epoch=epoch)
                        elif begin-last_unconfirmed_seen>=6:
                            self.post(bag_status='等待再次打开咕噜球背包' if self.inventory.initialized else '等待首次打开咕噜球背包',bag_epoch=epoch)
                    self.stop.wait(max(.05,BAG_PERIOD-(time.monotonic()-begin)))
                except Exception as error:
                    if isinstance(error,EvidenceError):
                        self.post(fatal=traceback.format_exc());return
                    try:self.evidence.record('bag_error',session,error=traceback.format_exc())
                    except Exception:
                        self.post(fatal=traceback.format_exc());return
                    stable.reset();self.post(bag_status=f'背包识别暂不可用：{str(error)[:60]}');self.stop.wait(1)
                    if sct:sct.close();sct=None
        finally:
            if sct:sct.close()

    def handle_bag(self, item):
        if 'bag_epoch' in item and item['bag_epoch']!=self.bag_epoch:
            if item.get('evidence'):self.evidence.record('stale_bag_ignored',self.session_id,evidence=item['evidence'])
            return True
        if 'bag_note' in item:
            self.note(item['bag_note']);return True
        if 'bag_status' in item:
            self.bag_status.config(text=item['bag_status'],fg=GREEN if item.get('bag_ok') else MUTED);return True
        if 'bag_snapshot' not in item:return False
        for key,icon in item['bag_icons'].items():self.add_ball_row(key,icon)
        amounts=item['bag_snapshot']
        allow_zero=item['bag_compact']
        if allow_zero:
            amounts=dict(amounts)
            for key in self.bag_view.keys():amounts.setdefault(key,0)
        before=copy.deepcopy(self.inventory.rows)
        changed=self.inventory.apply(amounts,allow_missing_zero=allow_zero)
        # Every stable opening is recorded, even if it confirms unchanged quantities.
        self.store.save_bag(self.session_id,self.inventory.rows,self.session_meteors)
        self.evidence.record('bag_applied',self.session_id,evidence=item.get('evidence'),changed=changed,
            observed=item['bag_snapshot'],allow_missing_zero=allow_zero,before=before,after=self.inventory.rows)
        for key,row in self.inventory.rows.items():
            self.bag_view.set_amounts(key,row['initial'],row['used'])
        self.note('背包已核对'+(' · 已保存截图' if item.get('evidence') else ''))
        self.update_totals()
        self.bag_status.config(text=f'已核对 {len(item["bag_snapshot"])} 种球',fg=GREEN)
        return True

    def apply_topmost(self):
        if not self.closing:
            self.root.attributes('-topmost',self.topmost.get())

    def _place_on_free_monitor(self):
        try:
            with mss.MSS() as sct:
                monitors=sct.monitors
                target,_=find_game(monitors)
                choices=[m for i,m in enumerate(monitors[1:],1) if not target or i!=target.monitor]
                monitor=choices[0] if choices else monitors[target.monitor if target else 1]
                self.root.update_idletasks()
                left,top,right,bottom=monitor_work_area(monitor)
                hwnd=ctypes.windll.user32.GetAncestor(self.root.winfo_id(),2)
                outer=wintypes.RECT();client=wintypes.RECT()
                user32=ctypes.windll.user32
                if not user32.GetWindowRect(ctypes.c_void_p(hwnd),ctypes.byref(outer)):
                    raise ctypes.WinError()
                if not user32.GetClientRect(ctypes.c_void_p(hwnd),ctypes.byref(client)):
                    raise ctypes.WinError()
                border_width=(outer.right-outer.left)-(client.right-client.left)
                border_height=(outer.bottom-outer.top)-(client.bottom-client.top)
                # A withdrawn Tk window can still report its temporary 200x200
                # size. Position the requested final client size plus its frame.
                width=min(self._startup_size[0],right-left-border_width)
                height=min(self._startup_size[1],bottom-top-border_height)
                minimum=self.root.minsize()
                self.root.minsize(min(minimum[0],width),min(minimum[1],height))
                x=left+round((right-left-width-border_width)*.5)
                y=top+round((bottom-top-height-border_height)*.45)
                # '+-x' remains relative to the left edge, unlike '-x', which
                # means an offset from the right. This supports negative monitors.
                self.root.geometry(f'{width}x{height}+{x}+{y}')
        except Exception:
            pass

    def post(self, **message):
        self.messages.put(message)

    def monitor_loop(self):
        tracker=EventTracker(self.persisted_active,self.persisted_shower_active,self.threshold)
        gate=tracker.meteor
        samples=deque(maxlen=3)
        sampling_times=deque(maxlen=16)
        pending=None
        def finish_pending():
            if pending:self.evidence.finish(pending['manifest'])
        previous_target=None
        last_epoch=self.bag_epoch
        sct=None
        try:
            detector=EventDetector()
            sct=mss.MSS()
            last_display_refresh=time.monotonic()
            while not self.stop.is_set():
                begin=time.monotonic()
                epoch,session=self.round_context()
                if epoch!=last_epoch:
                    tracker.unknown();samples.clear();sampling_times.clear();finish_pending();pending=None;last_epoch=epoch
                if not self.running.is_set():
                    tracker.unknown();samples.clear();sampling_times.clear();finish_pending();pending=None
                    self.stop.wait(.15)
                    continue
                try:
                    if begin-last_display_refresh>5:
                        sct.close(); sct=mss.MSS()
                        last_display_refresh=begin
                    monitors=sct.monitors
                    target,problem=find_game(monitors)
                    if target is None:
                        tracker.unknown();samples.clear();sampling_times.clear();finish_pending();pending=None
                        self.post(status=problem,valid=False)
                        self.stop.wait(.4); continue
                    identity=(target.hwnd,target.left,target.top,target.width,target.height)
                    if identity!=previous_target:
                        tracker.unknown();samples.clear();sampling_times.clear();finish_pending();pending=None;previous_target=identity
                    region=detector.region(target.width,target.height)
                    monitor=monitors[target.monitor]
                    name=monitor.get('name') or f'屏幕 {target.monitor}'
                    screen=f'{name}\n{target.width} × {target.height}'+(' · 主屏' if monitor.get('is_primary') else '')
                    if not region_visible(target,region,monitors[0]):
                        tracker.unknown();samples.clear();sampling_times.clear();finish_pending();pending=None
                        self.post(status='提示区域被遮挡，等待画面恢复',screen=screen,valid=False)
                        self.stop.wait(.25); continue
                    capture_started=time.monotonic()
                    capture_wall=datetime.now().astimezone().isoformat(timespec='milliseconds')
                    frame=capture_region(sct,target,region)
                    # Re-check layout after capture to reject window-movement races.
                    refreshed,_=find_game(monitors)
                    if refreshed != target or not region_visible(target,region,monitors[0]):
                        tracker.unknown();samples.clear();sampling_times.clear();finish_pending();pending=None;self.stop.wait(.1);continue
                    if frame.std()<2.:
                        tracker.unknown();samples.clear();sampling_times.clear();finish_pending();pending=None
                        self.post(status='截取到空白画面，请使用无边框或窗口模式',screen=screen,valid=False)
                        self.stop.wait(.4); continue
                    scene=detector.detect(frame,target.height,region[:2])
                    match=scene.meteor
                    now=time.monotonic()
                    kind=tracker.update(scene,now)
                    sampling_times.append(capture_started)
                    span=sampling_times[-1]-sampling_times[0]
                    sampling_fps=(len(sampling_times)-1)/span if span>0 else None
                    metadata=dict(time=capture_wall,monotonic=capture_started,match=asdict(match),
                                  shower_match=asdict(scene.shower),tip_score=scene.tip_score)
                    # Pause may be requested during matching; retain already detected event.
                    elapsed=time.monotonic()-begin
                    status=('检测到游戏 tips，等待横幅恢复' if scene.tip_score>=.82 else
                            '流星雨已计数，等待动画消失' if tracker.shower.active else
                            '陨星已计数，等待动画消失' if gate.active else '正在监测陨星和流星雨')
                    payload=dict(valid=True,score=match.score,shower_score=scene.shower.score,event=kind=='meteor',
                        shower_event=kind=='shower',active=gate.active,shower_active=tracker.shower.active,
                        screen=screen,round_epoch=epoch,status=status,elapsed=elapsed,sampling_fps=sampling_fps,
                        detected_monotonic=capture_started,detected_time=capture_wall)
                    if kind:
                        finish_pending()
                        frames=[(f'before_{i+1:02}',pixels,meta) for i,(pixels,meta) in enumerate(samples)]
                        frames.append(('trigger',frame,metadata))
                        selected=scene.shower if kind=='shower' else match
                        manifest=self.evidence.save(kind,session,frames,target=asdict(target),region=region,
                            threshold=gate.threshold,release_seconds=gate.release_seconds,match=asdict(selected),
                            meteor_match=asdict(match),shower_match=asdict(scene.shower),tip_score=scene.tip_score,
                            confirmation=dict(min_frames=3,min_seconds=.15,consistent_position=True,
                                              max_center_drift_text_heights=1.,min_size_ratio=.85),requested_after_frames=2)
                        payload['evidence']=manifest
                        pending=dict(manifest=manifest,started=now,times=deque([.35,.85]),index=0) if manifest else None
                    elif pending and now-pending['started']>=pending['times'][0]:
                        pending['times'].popleft();pending['index']+=1
                        self.evidence.add_frame(pending['manifest'],f'after_{pending["index"]:02}',frame,**metadata)
                        if not pending['times']:pending=None
                    if self.evidence.save_images:samples.append((frame,metadata))
                    else:samples.clear()
                    self.post(**payload)
                    self.stop.wait(max(.005,METEOR_PERIOD-elapsed))
                except Exception as error:
                    tracker.unknown();samples.clear();sampling_times.clear();finish_pending();pending=None
                    if isinstance(error,EvidenceError):
                        self.post(fatal=traceback.format_exc());return
                    try:self.evidence.record('meteor_error',session,error=traceback.format_exc())
                    except Exception:
                        self.post(fatal=traceback.format_exc());return
                    self.post(status=f'截图暂不可用：{str(error)[:85]}',valid=False)
                    # Recreate the capture object on the next attempt (display changes).
                    self.stop.wait(1.0)
                    try: sct.close()
                    except Exception: pass
                    sct=mss.MSS()
        except Exception:
            self.post(fatal=traceback.format_exc())
        finally:
            finish_pending()
            if sct:sct.close()

    def poll(self):
        try:
            for _ in range(60):
                try: item=self.messages.get_nowait()
                except queue.Empty: break
                if 'fatal' in item:
                    raise RuntimeError(item['fatal'])
                if 'diagnostic_usage' in item:
                    usage=item['diagnostic_usage']
                    self.diagnostic_usage.config(text=f"诊断截图 {usage['evidence_bytes']/1048576:.1f} / 500 MiB · 日志 {usage['log_bytes']/1048576:.1f} MiB",fg=MUTED)
                    continue
                if 'diagnostic_error' in item:
                    self.diagnostic_usage.config(text=item['diagnostic_error'],fg=GOLD);continue
                if self.handle_bag(item):continue
                if item.get('round_epoch',self.bag_epoch)!=self.bag_epoch:
                    if item.get('event'):self.evidence.record('stale_meteor_ignored',self.session_id,evidence=item.get('evidence'))
                    if item.get('shower_event'):self.evidence.record('stale_shower_ignored',self.session_id,evidence=item.get('evidence'))
                    continue
                if item.get('shower_event'):
                    self.commit_shower(item)
                elif item.get('event'):
                    self.commit_meteor(item)
                if 'active' in item and item['active']!=self.persisted_active:
                    self.store.set_active(item['active']); self.persisted_active=item['active']
                if 'shower_active' in item and item['shower_active']!=self.persisted_shower_active:
                    self.store.set_shower_active(item['shower_active']);self.persisted_shower_active=item['shower_active']
                if self.running.is_set():
                    self.status_label.config(text=item.get('status',''),fg=GREEN if item.get('valid') else '#efc58f')
                    if 'screen' in item: self.screen_label.config(text=item['screen'])
                    if item.get('valid'):
                        rate=item.get('sampling_fps')
                        rate_text=f'{rate:.1f} 次/秒' if rate is not None else '测量中'
                        self.score_label.config(text=f"陨星 {item['score']:.2f} · 流星雨 {item.get('shower_score',0.):.2f}\n单轮 {item['elapsed']*1000:.0f} ms · 实测 {rate_text}")
                    else:self.score_label.config(text='匹配分数 — · 等待有效画面')
        except Exception:
            self.failed=True; self.running.clear(); self.stop.set()
            self.status_label.config(text='程序已停止，请查看错误记录',fg='#ffafa6')
            self.pause_button.config(state='disabled')
            error=traceback.format_exc()
            try: (APP_DIR/'data'/'error.log').write_text(error,encoding='utf-8')
            except OSError: pass
            messagebox.showerror('陨星计数器',f'计数或记录保存发生错误，已停止识别，避免显示未保存的数字。\n\n{error[-700:]}',parent=self.root)
        if not self.stop.is_set(): self.root.after(70,self.poll)

    def toggle(self):
        if self.closing:return
        now=time.monotonic()
        if self.running.is_set():
            self.meteor_timing.pause(now)
            self.running.clear(); self.pause_button.config(text='继续计数')
            self.status_label.config(text='已暂停，累计记录已保留',fg=MUTED)
        else:
            self.meteor_timing.resume(now)
            self.running.set(); self.pause_button.config(text='暂停计数')
            self.status_label.config(text='正在恢复监测…',fg=GREEN)
        self.save_timing()
        self.evidence.record('monitor_paused' if self.meteor_timing.paused else 'monitor_resumed',
            self.session_id,since_last_seconds=self.meteor_timing.since_last(now))
        self.update_timing()

    def save_timing(self):
        state=self.meteor_timing.snapshot(time.monotonic())
        state['saved_wall']=datetime.now().astimezone().timestamp()
        self.store.save_timing(self.session_id,self.session_meteors,state)

    def _refresh_history(self):
        position=self.history.yview()[0]
        self.history.config(state='normal')
        self.history.delete('1.0','end')
        self.history.insert('1.0','\n'.join(self.recent_records) or '暂无记录，正在等待第一次提示')
        self.history.config(state='disabled')
        self.history.yview_moveto(position)

    def note(self, text):
        self.recent_records.appendleft(f'{datetime.now():%H:%M:%S}  {text}')
        self._refresh_history()

    def commit_meteor(self,item):
        self.session_meteors,when=self.store.event(item['score'],item['screen'],item.get('evidence'),
                                        session=self.session_id,when=item.get('detected_time'))
        self.persisted_active=True
        if self.round_started is None:
            self.round_started=when
            self.start_label.config(text=self.round_start_text())
        detected=item.get('detected_monotonic',time.monotonic())
        self.meteor_timing.add(detected)
        self.save_timing()
        self.evidence.record('meteor_counted',self.session_id,count=self.session_meteors,
            score=item['score'],screen=item['screen'],evidence=item.get('evidence'),detected_monotonic=detected,
            detected_time=when,round_started=self.round_started,
            recent_event_count=len(self.meteor_timing.events),recent_intervals_seconds=self.meteor_timing.intervals,
            average_interval_seconds=self.meteor_timing.average)
        self.last_label.config(text=f'最近一次  {when[11:19]}')
        self.note('陨星 +1'+(' · 已保存截图' if item.get('evidence') else ''))
        self.update_totals()
        self.update_timing()

    def commit_shower(self,item):
        self.session_showers,when=self.store.shower_event(self.session_id,item['shower_score'],item['screen'],
                                                        item.get('evidence'),when=item.get('detected_time'))
        self.persisted_shower_active=True
        self.evidence.record('shower_counted',self.session_id,count=self.session_showers,score=item['shower_score'],
            screen=item['screen'],evidence=item.get('evidence'),detected_time=when,
            detected_monotonic=item.get('detected_monotonic'))
        self.shower_last_label.config(text=f'最近一次  {when[11:19]}')
        self.note('流星雨 +1'+(' · 已保存截图' if item.get('evidence') else ''))
        self.update_totals()
        # Play only after the event is confirmed and saved; failures never erase it.
        try:
            play_shower_alert()
            self.alert_hint.config(text='触发时播放提示音',fg=MUTED)
        except (OSError,RuntimeError) as error:
            self.alert_hint.config(text='提示音不可用 · 请查看记录',fg=GOLD)
            self.evidence.record('shower_audio_error',self.session_id,error=str(error))

    def close(self):
        if self.closing:return
        self.closing=True
        self.running.clear(); self.stop.set()
        self.pause_button.config(state='disabled')
        self.status_label.config(text='正在保存记录并退出…',fg=MUTED)
        self._finish_close()

    def _finish_close(self):
        # A worker can still be encoding an already-confirmed event. Keep Tk
        # responsive and the database open until both producers have exited.
        self.worker.join(timeout=.05)
        self.bag_worker.join(timeout=.05)
        if self.worker.is_alive() or self.bag_worker.is_alive():
            self.root.after(50,self._finish_close)
            return
        # No producer can append after this final queue drain.
        while not self.messages.empty():
            item=self.messages.get_nowait()
            if self.handle_bag(item):continue
            if item.get('round_epoch',self.bag_epoch)!=self.bag_epoch:continue
            if item.get('shower_event'):
                self.commit_shower(item)
            elif item.get('event'):
                self.commit_meteor(item)
            if 'active' in item:self.store.set_active(item['active'])
            if 'shower_active' in item:self.store.set_shower_active(item['shower_active'])
        self.save_timing()
        self.evidence.record('app_closed',self.session_id,meteors=self.session_meteors,showers=self.session_showers,
                             paused=self.meteor_timing.paused)
        self.store.close(); self.root.destroy()

    def run(self):
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.mainloop()


def main():
    import argparse
    parser=argparse.ArgumentParser(description='陨星计数器')
    parser.add_argument('--resume-round',default=None,help='指定继续刚保存的轮次；默认自动继续上次轮次')
    parser.add_argument('--paused',action='store_true',help='调试升级时保留暂停状态')
    args=parser.parse_args()
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
    kernel.CreateMutexW.restype=ctypes.c_void_p
    mutex=kernel.CreateMutexW(None,False,'Local\\RockKingdomMeteorCounter_v1')
    if ctypes.get_last_error()==183:
        ctypes.windll.user32.MessageBoxW(None,'陨星计数器已经在运行，请查看另一个屏幕或任务栏。','陨星计数器',0x40)
        return
    try:
        legacy=(Path(sys.executable).parent if getattr(sys,'frozen',False)
                else Path(__file__).resolve().parent.parent/'runtime')/'data'
        import_legacy_data(legacy,APP_DIR/'data')
        CounterApp(resume_round=args.resume_round,start_paused=args.paused).run()
    except Exception:
        error=traceback.format_exc()
        try:
            (APP_DIR/'data').mkdir(parents=True,exist_ok=True)
            (APP_DIR/'data'/'error.log').write_text(error,encoding='utf-8')
        except OSError: pass
        ctypes.windll.user32.MessageBoxW(None,error[-1300:],'陨星计数器启动失败',0x10)
    finally:
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        if mutex: kernel.CloseHandle(mutex)


if __name__=='__main__':
    main()
