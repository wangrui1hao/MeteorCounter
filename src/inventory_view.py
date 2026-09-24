"""Two compact icon-only inventory columns for the native desktop UI."""
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk


class InventoryView(tk.Frame):
    def __init__(self, parent, background, surface, foreground, muted, accent, edit_price):
        super().__init__(parent, bg=background)
        self.icons = {}
        self.images = {}
        self.amounts = {}
        self.prices = {}
        self.edit_price = edit_price
        self.locations = {}
        self.tables = []
        self.editor = None
        self.edit_key = None
        self.edit_colors = dict(bg=surface,fg=foreground,insertbackground=foreground,
                                highlightbackground=accent,highlightcolor=accent)
        self.edit_hint = tk.Label(self,text='请输入非负整数，0 清除单价',bg=surface,fg='#ff9b9b',font=('Microsoft YaHei UI',9))
        self.winfo_toplevel().bind('<Button-1>',self._outside_click,add='+')
        style = ttk.Style(self)
        style.configure('Bag.Treeview', background=surface, fieldbackground=surface, foreground=foreground,
                        font=('Microsoft YaHei UI', 11), rowheight=48, borderwidth=0)
        style.configure('Bag.Treeview.Heading', font=('Microsoft YaHei UI', 10),
                        background='#243249', foreground=muted, borderwidth=0, padding=(5, 9))
        style.map('Bag.Treeview', background=[('selected', '#2b4057')])
        style.map('Bag.Treeview.Heading', background=[('active', '#243249')])
        self.columnconfigure((0, 1), weight=1, uniform='halves')
        self.rowconfigure(0, weight=1)
        for column in range(2):
            table = ttk.Treeview(self, style='Bag.Treeview', columns=('initial','used','price'),
                                 height=8, selectmode='none')
            table.heading('#0', text='咕噜球')
            table.column('#0', width=74, minwidth=64, stretch=False)
            for key, title in [('initial','初始'), ('used','消耗'), ('price','单价（双击）')]:
                table.heading(key, text=title)
                table.column(key, width=104, minwidth=80, anchor='center')
            table.tag_configure('even', background=surface)
            table.tag_configure('odd', background='#1b293e')
            table.grid(row=0, column=column, sticky='nsew', padx=(0, 12) if column == 0 else (0, 0))
            table.bind('<Double-1>',lambda event,t=table:self._edit_price(t,event))
            table.bind('<Button-1>',lambda event,t=table:self._table_click(t,event))
            table.bind('<Configure>',lambda event:self._position_editor())
            table.configure(yscrollcommand=lambda *args:self._position_editor())
            self.tables.append(table)

    def add(self, key, icon):
        if key in self.icons:
            return
        self.icons[key] = icon.copy()
        thumb = icon.copy()
        thumb.thumbnail((38, 38), Image.Resampling.LANCZOS)
        self.images[key] = ImageTk.PhotoImage(thumb)
        self.amounts[key] = (0, 0)
        self._arrange()

    def _arrange(self):
        keys = list(self.icons)
        half = (len(keys) + 1)//2
        for table in self.tables:
            if table.get_children():
                table.delete(*table.get_children())
            table.configure(height=max(1, half))
        self.locations.clear()
        for i, key in enumerate(keys):
            column, row = divmod(i, half)
            table = self.tables[column]
            table.insert('', 'end', iid=key, text='', image=self.images[key], values=self._values(key),
                         tags=('even' if row % 2 == 0 else 'odd',))
            self.locations[key] = table
        self._position_editor()

    def keys(self):
        return tuple(self.icons)

    def _values(self, key):
        return (*self.amounts[key],self.prices.get(key,'未设置'))

    def _edit_price(self, table, event):
        if self._table_click(table,event)=='break':return 'break'
        key=table.identify_row(event.y)
        if key and table.identify_column(event.x)=='#3':
            self.begin_edit(key)
            # Do not let this same click reach the toplevel outside-click handler.
            return 'break'

    def _table_click(self, table, event):
        if table.identify_region(event.x,event.y)=='separator':
            self._outside_click(event)
            return 'break'

    def begin_edit(self, key):
        if self.edit_key==key:return
        if self.editor is not None and not self.finish_edit(discard_invalid=True):return
        self.edit_key=key
        self.editor=tk.Entry(self,justify='center',font=('Microsoft YaHei UI',11),
                             relief='flat',highlightthickness=1,**self.edit_colors)
        self.editor.insert(0,str(self.prices.get(key,'')))
        self.editor.bind('<Return>',lambda event:self._commit_event())
        self.editor.bind('<KP_Enter>',lambda event:self._commit_event())
        self.editor.bind('<Escape>',lambda event:self._cancel_event())
        self.editor.bind('<FocusOut>',lambda event:self.finish_edit(discard_invalid=True))
        self._position_editor()
        self.editor.focus_set()
        self.editor.selection_range(0,'end')

    def _position_editor(self):
        if self.editor is None:return
        table=self.locations.get(self.edit_key)
        box=table.bbox(self.edit_key,'price') if table else ()
        self.edit_hint.place_forget()
        if not box:
            self.editor.place_forget();return
        x,y,w,h=box
        self.editor.place(x=table.winfo_x()+x+2,y=table.winfo_y()+y+2,width=w-4,height=h-4)

    def _outside_click(self, event):
        if self.editor is not None and event.widget!=self.editor:self.finish_edit(discard_invalid=True)

    def _commit_event(self):
        self.finish_edit()
        return 'break'

    def _cancel_event(self):
        self.cancel_edit()
        return 'break'

    def cancel_edit(self):
        editor=self.editor
        self.editor=None;self.edit_key=None
        self.edit_hint.place_forget()
        if editor is not None:editor.destroy()

    def finish_edit(self, discard_invalid=False):
        if self.editor is None:return True
        text=self.editor.get().strip()
        try:
            if not text.isascii() or not text.isdecimal():raise ValueError
            price=int(text)
            if price<0:raise ValueError
        except ValueError:
            if discard_invalid:
                self.cancel_edit()
                return True
            self.editor.configure(highlightbackground='#ff9b9b',highlightcolor='#ff9b9b')
            self.edit_hint.place(x=self.editor.winfo_x(),y=self.editor.winfo_y()+self.editor.winfo_height())
            self.edit_hint.lift()
            return False
        key=self.edit_key
        # Remove the editor before saving: dialogs on a storage error may emit FocusOut.
        self.cancel_edit()
        return self.edit_price(key,price)

    def set_price(self, key, price):
        if price==0:self.prices.pop(key,None)
        else:self.prices[key]=price
        self.locations[key].item(key,values=self._values(key))

    def set_amounts(self, key, initial, used):
        self.amounts[key] = (initial, used)
        self.locations[key].item(key, values=self._values(key))

    def reset(self):
        for key in self.icons:
            self.set_amounts(key, 0, 0)
