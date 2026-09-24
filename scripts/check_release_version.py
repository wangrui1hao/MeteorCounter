"""Reject releases unless the Git tag and both application versions agree."""
import ast
import re
import sys
from pathlib import Path


def check(ref, root):
    match = re.fullmatch(r"refs/tags/v(\d+\.\d+\.\d+)", ref)
    if not match:
        raise ValueError("Release requires a vMAJOR.MINOR.PATCH tag; select a tag for manual runs.")
    version = match[1]
    tree = ast.parse((root / "src/diagnostics.py").read_text(encoding="utf-8"))
    versions = [ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
    metadata = (root / "src/windows_version.txt").read_text(encoding="utf-8")
    strings = re.findall(r"StringStruct\('(FileVersion|ProductVersion)'\s*,\s*'([^']+)'\)", metadata)
    tuples = re.findall(r"(?:filevers|prodvers)\s*=\s*(\([^)]*\))", metadata)
    expected = tuple(map(int, version.split("."))) + (0,)
    if (versions != [version] or dict(strings) != {"FileVersion": version, "ProductVersion": version}
            or len(tuples) != 2 or any(ast.literal_eval(value) != expected for value in tuples)):
        raise ValueError("Tag, diagnostics.VERSION and Windows file/product versions must match.")
    notes=root/'docs/releases'/f'v{version}.md'
    if not notes.is_file() or not notes.read_text(encoding='utf-8-sig').strip():
        raise ValueError(f'Release notes are required: {notes}')
    return version


if __name__ == "__main__":
    print("Release version:", check(sys.argv[1], Path(__file__).resolve().parents[1]))
