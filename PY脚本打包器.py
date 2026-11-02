# -*- coding: utf-8 -*-
"""
Python 打包工具 — PyInstaller 图形化前端
═══════════════════════════════════════════════════════════════════
  主要变化
  ────────────────────────────────────────────────────────────────
  1. 常用参数全部搬到主页, 不用再翻标签页
  2. 输出目录旁边加了「选择…」按钮 + 「打开」
  3. 选完文件/目录自动做 AST 模块扫描
     · 支持 import / from ... import / __import__() / import_module()
     · 三种策略: 自动全部打入 / 手动勾选 / 不打包模块(仅提醒)
     · 会标出「解释器里找不到」的模块 —— 这类模块是 exe 启动即崩的主因
  4. Python 解释器可选: 默认用启动本脚本的那个
     · 一键扫描本机所有 Python 变成下拉列表
     · 也能手填/exe 浏览任意解释器(含嵌入式)
     · 下方常驻小字提示嵌入式 Python 的库缺失风险
  5. 目录模式重做: 选「整个项目目录 + 入口脚本」→ 输出单个 exe,
     双击就执行入口脚本, 项目里的依赖/资源全都打进去
  6. 修掉 exe 启动报「从服务器返回了一个参照」的致命 bug
     （原因: --uac-uiaccess 会给 exe 写 uiAccess="true" 清单,
       未签名/不在 Program Files 的程序被系统直接拒绝启动）
  7. 构建临时文件不再拉在桌面, 统一进 %LOCALAPPDATA%\\PyPacker
═══════════════════════════════════════════════════════════════════
"""
import ast
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import (
    Tk, Toplevel, Frame, Label, Button, Entry, Listbox, Radiobutton, Checkbutton,
    filedialog, messagebox, StringVar, IntVar, BooleanVar, Scrollbar, Text, Menu,
    Canvas, EXTENDED, END, NORMAL, DISABLED, WORD, VERTICAL, BOTH, LEFT, RIGHT,
    TOP, X, Y, BOTTOM,
)
from tkinter import ttk

APP_TITLE = 'Python 打包工具  ·  PyInstaller 图形前端'
CONFIG_VERSION = 4
BUILD_LOG_LINES = 800
BUILD_TIMEOUT = 2400

if getattr(sys, 'frozen', False):
    # 打包成 exe 后 __file__ 指向临时解包目录，配置写那儿等于每次启动都丢
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _config_dir():
    """优先把配置放在程序旁边；那个目录不可写就退到 LOCALAPPDATA"""
    try:
        probe = os.path.join(SCRIPT_DIR, '.write_probe')
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
        return SCRIPT_DIR
    except Exception:
        fallback = os.path.join(
            os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), 'PyPacker')
        try:
            os.makedirs(fallback, exist_ok=True)
        except OSError:
            pass
        return fallback


CONFIG_PATH = os.path.join(_config_dir(), '.py_packer_config.json')

# 轻量模式的外挂模块。故意做成「可选」：
# 这个 .py 被删掉、损坏、或跟主程序分开放的，主程序照样能启动，只是模式5不可用。
LIGHT_MODULE = 'light_stub'
LIGHT_SUFFIX = '-需PY'


def light_search_dirs():
    """按优先级列出可能放着 light_stub.py 的目录"""
    dirs = [SCRIPT_DIR]
    if getattr(sys, 'frozen', False):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            dirs.append(meipass)
    dirs.append(os.getcwd())
    out, seen = [], set()
    for d in dirs:
        if not d:
            continue
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def load_light_module():
    """
    返回 (模块 或 None, 状态说明)
    任何异常都吞掉 —— 绝不能让外挂模块拖垮主程序。
    """
    for folder in light_search_dirs():
        path = os.path.join(folder, LIGHT_MODULE + '.py')
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location(LIGHT_MODULE, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module, f'已加载 {path}'
        except Exception as exc:
            return None, f'{path} 载入失败: {exc}'

    try:
        import light_stub as module          # 万一被打进包里了
        return module, '已加载(内置)'
    except Exception:
        pass

    return None, (f'缺少 {LIGHT_MODULE}.py —— 把它放在本程序同一个文件夹里就能启用模式5')


NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 项目目录里不该打进 exe 的东西
JUNK_DIRS = {
    '__pycache__', 'build', 'dist', '.git', '.svn', '.hg', '.idea', '.vscode',
    'node_modules', '.venv', 'venv', 'env', 'ENV', '.mypy_cache', '.pytest_cache',
    '.tox', '.ruff_cache', 'site-packages', '.eggs', 'htmlcov', '.ipynb_checkpoints',
}
JUNK_EXTS = {'.pyc', '.pyo', '.pdb', '.spec', '.log', '.tmp', '.bak'}

# 只有「精确命中模块名」才追加的隐藏导入, 用来兜住那些靠动态导入吃子模块的库
EXTRA_HIDDEN = {
    'PIL': ['PIL._tkinter_finder'],
    'matplotlib': ['matplotlib.backends.backend_tkagg', 'matplotlib.backends.backend_agg'],
    'pandas': ['pandas._libs.tslibs.base', 'pandas._libs.tslibs.np_datetime'],
    'sklearn': ['sklearn.utils._typedefs', 'sklearn.utils._heap',
                'sklearn.utils._sorting', 'sklearn.utils._vector_sentinel'],
    'openpyxl': ['openpyxl.cell._writer'],
    'pydantic': ['pydantic.deprecated.decorator'],
    'pkg_resources': ['pkg_resources'],
    'yaml': ['yaml'],
    'bs4': ['bs4'],
    'dateutil': ['dateutil.parser', 'dateutil.tz'],
    'google.protobuf': ['google.protobuf'],
    'PyQt5': ['PyQt5.sip'],
    'PySide6': ['shiboken6'],
    'pymysql': ['pymysql.constants'],
    'psycopg2': ['psycopg2._psycopg'],
}

VERSION_FILE_TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
    subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'{company}'),
         StringStruct(u'FileDescription', u'{desc}'),
         StringStruct(u'FileVersion', u'{version}'),
         StringStruct(u'InternalName', u'{internal}'),
         StringStruct(u'LegalCopyright', u'{copyright}'),
         StringStruct(u'OriginalFilename', u'{orig}'),
         StringStruct(u'ProductName', u'{product}'),
         StringStruct(u'ProductVersion', u'{version}')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [0x0409, 0x04B0])])
  ]
)
"""

# 用来问「这个模块在解释器里存在吗 / 是不是标准库 / 版本多少」
# 不 import, 只用 find_spec + metadata, 避免执行目标库的代码
MODULE_PROBE = r'''
import sys, json
from importlib import util as _util

try:
    names = json.loads(sys.stdin.read() or "[]")
except Exception:
    names = []

stdlib = set(getattr(sys, "stdlib_module_names", ()))
builtin = set(sys.builtin_module_names)

try:
    from importlib import metadata as _md
except Exception:
    _md = None

res = {}
for n in names:
    info = {"stdlib": (n in stdlib) or (n in builtin), "found": False,
            "file": "", "version": "", "namespace": False}
    try:
        spec = _util.find_spec(n)
    except Exception:
        spec = None
    if spec is not None:
        info["found"] = True
        info["file"] = spec.origin or ""
        info["namespace"] = (spec.origin in (None, "", "namespace"))
    if info["found"] and not info["stdlib"] and _md is not None:
        try:
            info["version"] = _md.version(n.split(".")[0]) or ""
        except Exception:
            info["version"] = ""
    res[n] = info

sys.stdout.write("@@MOD@@" + json.dumps(res))
'''

PYINFO_PROBE = (
    'import sys, json; print("@@PY@@" + json.dumps({"v": sys.version.split()[0], '
    '"bits": 64 if sys.maxsize > 2**32 else 32, "prefix": sys.prefix}))'
)


# ══════════════════════════════════════════════════════════════
#  基础工具
# ══════════════════════════════════════════════════════════════
def clean_env():
    """
    给子进程一套干净的环境。
    PYTHONPATH / PYTHONHOME 这类变量如果被 IDE、启动器或其他软件塞了东西,
    PyInstaller 会把那些目录也当成模块搜索路径, 轻则多打一堆无关模块,
    重则和标准库/第三方库打架, 打出来的 exe 反而起不来。
    """
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP',
                'PYTHONEXECUTABLE', 'PYTHONUSERBASE'):
        env.pop(key, None)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return env


def run_capture(cmd, timeout=20, cwd=None, stdin_text=None):
    """跑个命令把输出抓回来, 不弹黑窗。返回 (返回码, 合并输出)

    超时用 _kill 杀整棵进程树：subprocess.run 的 timeout 只杀直接子进程，
    孙进程会变孤儿继续跑（pip 的构建后端、解释器探测派生出来的进程都是）。
    """
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace', cwd=cwd,
            env=clean_env(), creationflags=NO_WINDOW)
        out, _ = proc.communicate(input=stdin_text, timeout=timeout)
        return proc.returncode, out or ''
    except subprocess.TimeoutExpired:
        if proc is not None:
            _kill(proc)
        return -9, '超时'
    except Exception as exc:
        return -1, str(exc)


def read_text(path):
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            with open(path, 'r', encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return ''
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except OSError:
        return ''


def is_embedded_python(exe):
    """嵌入式(embeddable) Python 旁边会有 python3xx._pth"""
    try:
        folder = os.path.dirname(os.path.abspath(exe))
        for name in os.listdir(folder):
            if name.lower().endswith('._pth'):
                return True
    except OSError:
        pass
    return False


def python_info(exe):
    """返回 {exe, version, bits, embedded, pyinstaller} 或 None"""
    if not exe or not os.path.isfile(exe):
        return None
    rc, out = run_capture([exe, '-c', PYINFO_PROBE], timeout=20)
    if rc != 0:
        return None
    info = None
    for line in out.splitlines():
        if line.startswith('@@PY@@'):
            try:
                info = json.loads(line[len('@@PY@@'):])
            except Exception:
                info = None
    if not info:
        return None
    ver = ''
    rc2, out2 = run_capture([exe, '-m', 'PyInstaller', '--version'], timeout=40)
    if rc2 == 0:
        ver = (out2.strip().splitlines() or [''])[-1].strip()
    return {
        'exe': os.path.abspath(exe),
        'version': info.get('v', '?'),
        'bits': info.get('bits', '?'),
        'prefix': info.get('prefix', ''),
        'embedded': is_embedded_python(exe),
        'pyinstaller': ver,
    }


def candidate_pythons():
    """把本机上能找到的 python.exe 全捞出来(不扫全盘, 只扫常规位置)"""
    cands = []

    if sys.executable:
        cands.append(sys.executable)

    launcher = shutil.which('py')
    if launcher:
        rc, out = run_capture([launcher, '-0p'], timeout=20)
        if rc == 0:
            for line in out.splitlines():
                m = re.search(r'([A-Za-z]:\\[^\r\n]*?python[w]?\.exe)', line)
                if m:
                    cands.append(m.group(1).strip())

    try:
        import winreg
        keys = [
            (winreg.HKEY_CURRENT_USER, r'Software\Python\PythonCore'),
            (winreg.HKEY_LOCAL_MACHINE, r'Software\Python\PythonCore'),
            (winreg.HKEY_LOCAL_MACHINE, r'Software\WOW6432Node\Python\PythonCore'),
        ]
        for hive, path in keys:
            try:
                root = winreg.OpenKey(hive, path)
            except OSError:
                continue
            idx = 0
            while True:
                try:
                    tag = winreg.EnumKey(root, idx)
                except OSError:
                    break
                idx += 1
                for sub in ('InstallPath', r'InstallPath\ExecutablePath'):
                    try:
                        with winreg.OpenKey(root, tag + '\\' + sub) as k:
                            try:
                                val = winreg.QueryValueEx(k, 'ExecutablePath')[0]
                            except OSError:
                                val = winreg.QueryValueEx(k, '')[0]
                    except OSError:
                        continue
                    if val:
                        val = str(val)
                        if val.lower().endswith('.exe'):
                            cands.append(val)
                        else:
                            cands.append(os.path.join(val, 'python.exe'))
    except Exception:
        pass

    for name in ('python', 'python3', 'pythonw'):
        p = shutil.which(name)
        if p:
            cands.append(p)

    home = os.path.expanduser('~')
    local = os.environ.get('LOCALAPPDATA', os.path.join(home, 'AppData', 'Local'))
    patterns = [
        os.path.join(local, 'Programs', 'Python', 'Python*', 'python.exe'),
        os.path.join(local, 'Programs', 'Python', 'Python*', 'pythonw.exe'),
        os.path.join(local, 'Microsoft', 'WindowsApps', 'python*.exe'),
        os.path.join(home, 'anaconda3', 'python.exe'),
        os.path.join(home, 'miniconda3', 'python.exe'),
        os.path.join(home, 'AppData', 'Local', 'anaconda3', 'python.exe'),
        os.path.join(home, 'AppData', 'Local', 'miniconda3', 'python.exe'),
        r'C:\Python*\python.exe',
        r'C:\Program Files\Python*\python.exe',
        r'C:\Program Files (x86)\Python*\python.exe',
        os.path.join(local, 'Programs', 'Python', '*', 'python.exe'),
    ]
    import glob as _g
    for pat in patterns:
        try:
            cands.extend(_g.glob(pat))
        except Exception:
            pass

    seen = set()
    out = []
    for c in cands:
        if not c or not os.path.isfile(c):
            continue
        try:
            key = os.path.normcase(os.path.realpath(c))
        except OSError:
            key = os.path.normcase(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(os.path.abspath(c))
    return out


def scan_interpreters(log=None):
    result = []
    for exe in candidate_pythons():
        info = python_info(exe)
        if not info:
            continue
        if log:
            log(f"[扫描] {info['exe']}  Python {info['version']}"
                f"{' (嵌入式)' if info['embedded'] else ''}"
                f"{'  PyInstaller ' + info['pyinstaller'] if info['pyinstaller'] else '  无PyInstaller'}")
        result.append(info)

    def _key(item):
        try:
            parts = [int(x) for x in item['version'].split('.')[:3]]
        except ValueError:
            parts = [0]
        return (item['exe'] != os.path.abspath(sys.executable),
                -1 if item['pyinstaller'] else 0,
                [-p for p in parts])

    result.sort(key=_key)
    return result


# ══════════════════════════════════════════════════════════════
#  模块扫描
# ══════════════════════════════════════════════════════════════
_SCAN_TRUNCATED = False


def scan_truncated():
    return _SCAN_TRUNCATED


def collect_py_files(target):
    global _SCAN_TRUNCATED
    _SCAN_TRUNCATED = False
    target = os.path.abspath(target)
    if os.path.isfile(target):
        return [target]
    found = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d.lower() not in JUNK_DIRS
                   and not d.startswith('.')]
        for name in files:
            if name.lower().endswith(('.py', '.pyw')):
                found.append(os.path.join(root, name))
        # 以前到 4000 就静默 break，用户只看到"4001 个文件"，不知道被截断了。
        # 现在放宽上限并打上标记，界面上会明确提示清单可能不全。
        if len(found) > 20000:
            _SCAN_TRUNCATED = True
            break
    return found


def _dotted(node):
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return '.'.join(reversed(parts))
    return ''


DYNAMIC_IMPORTERS = {'__import__', 'importlib.import_module', 'import_module',
                     'importlib.__import__', 'imp.load_module'}


def analyze_imports(paths):
    """
    返回 (tops, subs, local_names, errors)
      tops  : 真正的顶层依赖 (import x / from x import ... / __import__('x'))
      subs  : from X import Y 展开出来的候选子模块 X.Y
              —— 只是候选, Y 可能是类/函数, 所以后面要验证它是真模块
      local : 看起来是项目自己的模块
      errors: 解析失败的文件
    """
    tops = set()
    subs = set()
    local = set()
    errors = []

    py_dirs = set()
    for p in paths:
        stem = Path(p).stem
        if stem and stem != '__init__':
            local.add(stem)
        py_dirs.add(os.path.dirname(p))
    for d in list(py_dirs):
        try:
            for name in os.listdir(d):
                full = os.path.join(d, name)
                if os.path.isdir(full) and os.path.isfile(os.path.join(full, '__init__.py')):
                    local.add(name)
                elif name.lower().endswith(('.py', '.pyw')):
                    # 同目录的脚本兄弟模块: 由 PyInstaller 自动收集, 不算第三方依赖
                    stem = os.path.splitext(name)[0]
                    if stem != '__init__':
                        local.add(stem)
        except OSError:
            pass

    for p in paths:
        src = read_text(p)
        if not src:
            continue
        try:
            tree = ast.parse(src, filename=p)
        except SyntaxError as exc:
            # 二进制文件或解码失败的源码, exc.lineno 可能是 None, 别写成「第None行」
            line = exc.lineno if exc.lineno is not None else '?'
            errors.append(f'{os.path.basename(p)}: 第{line}行语法错误')
            continue
        except Exception as exc:
            errors.append(f'{os.path.basename(p)}: {exc}')
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name:
                        tops.add(alias.name)
                        tops.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # from . import xxx → 项目内部相对导入
                    continue
                mod = node.module or ''
                if mod:
                    tops.add(mod)
                    tops.add(mod.split('.')[0])
                    for alias in node.names:
                        if alias.name and alias.name != '*':
                            # 可能是子模块, 也可能只是包里的一个类/函数
                            subs.add(mod + '.' + alias.name)
            elif isinstance(node, ast.Call):
                fname = _dotted(node.func)
                if fname in DYNAMIC_IMPORTERS:
                    target = None
                    if node.args and isinstance(node.args[0], ast.Constant):
                        val = node.args[0].value
                        if isinstance(val, str):
                            target = val
                    if target:
                        tops.add(target)
                        tops.add(target.split('.')[0])

    tops = {n for n in tops if n and not n.startswith('.')}
    subs = {n for n in subs if n and not n.startswith('.')}
    subs -= tops
    return tops, subs, local, errors


def probe_modules(py_exe, names):
    """返回 {名字: 信息}；探测本身失败时返回 None。

    必须区分「探测成功但这个模块没有」和「解释器根本没能跑起来」：
    后者以前会退化成"所有名字都缺失"，界面上把 os / sys / json 全标红，
    还提示用户去 pip install os —— 真因（解释器坏了）完全被掩盖。
    """
    names = sorted(n for n in names if n)
    if not names:
        return {}
    if not py_exe or not os.path.isfile(py_exe):
        return None
    rc, out = run_capture([py_exe, '-c', MODULE_PROBE], timeout=180,
                          stdin_text=json.dumps(names))
    for line in out.splitlines():
        if line.startswith('@@MOD@@'):
            try:
                return json.loads(line[len('@@MOD@@'):])
            except Exception:
                return None
    return None


def classify_modules(py_exe, tops, subs, local_names):
    """返回 {'third', 'stdlib', 'local', 'missing', 'probe_failed'}"""
    probed = probe_modules(py_exe, tops)
    if probed is None:
        return {'third': {}, 'stdlib': {}, 'local': {}, 'missing': {},
                'probe_failed': True}

    std_roots = {n.split('.')[0] for n, i in probed.items() if i.get('stdlib')}

    # 只验证「非标准库、非项目内」的 from X import Y 候选, 不浪费时间去探标准库的子项
    todo = [s for s in subs
            if s.split('.')[0] not in std_roots
            and s.split('.')[0] not in local_names]
    if todo:
        extra = probe_modules(py_exe, todo)
        if extra:
            probed.update(extra)

    third, stdlib_, local_, missing = {}, {}, {}, {}
    for name in sorted(tops):
        info = probed.get(name) or {}
        root = name.split('.')[0]
        if info.get('stdlib') or root in std_roots:
            stdlib_[name] = info
        elif root in local_names:
            local_[name] = info
        elif info.get('found'):
            third[name] = info
        else:
            missing[name] = info

    for name in sorted(subs):
        root = name.split('.')[0]
        if root in std_roots or root in local_names:
            continue
        info = probed.get(name) or {}
        # 只有本身就是模块(找得到)才收；找不到说明 Y 是类/函数, 不算缺失
        if info.get('found') and name not in third:
            third[name] = info

    return {'third': third, 'stdlib': stdlib_, 'local': local_,
            'missing': missing, 'probe_failed': False}


# ══════════════════════════════════════════════════════════════
#  打包命令
# ══════════════════════════════════════════════════════════════
def work_root():
    base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    path = os.path.join(base, 'PyPacker')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = tempfile.gettempdir()
    return path


HOOK_CHDIR = """# 运行时把工作目录切到 PyInstaller 解包出来的资源目录
# 这样源码里写的相对路径(open('config.json') 之类)在 exe 里也能找到文件
import os
import sys

if getattr(sys, 'frozen', False):
    _base = getattr(sys, '_MEIPASS', None)
    if _base and os.path.isdir(_base):
        try:
            os.chdir(_base)
        except OSError:
            pass
"""


def under_any(path, roots):
    """path 是否落在 roots 里任意一个目录之下（含自身）"""
    if not roots:
        return False
    try:
        np = os.path.normcase(os.path.realpath(path))
    except OSError:
        np = os.path.normcase(os.path.abspath(path))
    for r in roots:
        if not r:
            continue
        try:
            nr = os.path.normcase(os.path.realpath(r))
        except OSError:
            nr = os.path.normcase(os.path.abspath(r))
        if np == nr or np.startswith(nr + os.sep):
            return True
    return False


def project_data_pairs(root, exclude_paths=()):
    """把项目根目录的每一项做成 --add-data 的 (源, 目标)

    目录的「目标」必须是它自己的名字。PyInstaller 对目录型源路径的语义是
    「把该目录里的内容铺进目标目录」——dest 传 '.' 会把 assets/logo.png
    拍平成根目录的 logo.png，程序里 open('assets/logo.png') 就找不到了；
    不同子目录里的同名文件还会互相覆盖。
    """
    root = os.path.abspath(root)
    skip = [p for p in exclude_paths if p]
    pairs = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return pairs

    for name in entries:
        full = os.path.join(root, name)
        # 输出目录/构建目录可能在项目更深处，必须按前缀排除，不能只比顶层
        if under_any(full, skip):
            continue
        low = name.lower()
        if os.path.isdir(full):
            if low in JUNK_DIRS or low.startswith('.'):
                continue
            pairs.append((full, name))
        else:
            if os.path.splitext(low)[1] in JUNK_EXTS:
                continue
            pairs.append((full, '.'))
    return pairs


def find_nested_junk(root, limit=6):
    """找出项目里被 --add-data 整目录带走的垃圾子目录（__pycache__/.git 等）"""
    hits = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return hits
    for name in entries:
        full = os.path.join(root, name)
        if not os.path.isdir(full) or name.lower() in JUNK_DIRS or name.startswith('.'):
            continue
        for cur, dirs, _files in os.walk(full):
            for d in list(dirs):
                if d.lower() in JUNK_DIRS or d.startswith('.'):
                    hits.append(os.path.join(cur, d))
                    dirs.remove(d)
                    if len(hits) >= limit:
                        return hits
    return hits


def build_pyinstaller_cmd(py_exe, entry, cfg):
    """按配置拼 PyInstaller 命令。返回 (cmd, 说明列表)"""
    notes = []
    tmp_root = cfg['workpath']
    cmd = [py_exe, '-m', 'PyInstaller']
    cmd += ['--noconfirm', '--log-level', 'INFO']
    cmd += ['--distpath', cfg['distpath']]
    cmd += ['--workpath', tmp_root]
    cmd += ['--specpath', tmp_root]
    cmd += ['--name', cfg['name']]
    cmd.append('--onefile' if cfg['onefile'] else '--onedir')
    cmd.append('--noconsole' if cfg['noconsole'] else '--console')

    if cfg.get('icon') and os.path.isfile(cfg['icon']):
        cmd += ['--icon', cfg['icon']]
    if cfg.get('version_file') and os.path.isfile(cfg['version_file']):
        cmd += ['--version-file', cfg['version_file']]
    if cfg.get('clean'):
        cmd.append('--clean')
    if cfg.get('uac_admin'):
        cmd.append('--uac-admin')
        notes.append('已写入管理员权限清单(启动会弹 UAC)')
    if cfg.get('debug'):
        cmd += ['--debug', cfg['debug']]
    if cfg.get('upx_dir') and os.path.isdir(cfg['upx_dir']):
        cmd += ['--upx-dir', cfg['upx_dir']]
    if cfg.get('runtime_tmpdir'):
        cmd += ['--runtime-tmpdir', cfg['runtime_tmpdir']]
    if cfg.get('disable_windowed_traceback'):
        cmd.append('--disable-windowed-traceback')

    splash = cfg.get('splash')
    if splash:
        if os.path.isfile(splash):
            cmd += ['--splash', splash]
        else:
            notes.append(f'启动画面文件不存在, 已忽略: {splash}')

    for hook in cfg.get('hooks', []):
        cmd += ['--runtime-hook', hook]
        notes.append(f'运行时钩子: {os.path.basename(hook)}')

    for path in cfg.get('paths', []):
        cmd += ['--paths', path]

    excludes = list(cfg.get('exclude', []))
    for mod in cfg.get('hidden', []):
        # 同一个模块既 hidden 又 exclude 时，PyInstaller 里 exclude 优先，
        # 界面上却还把它算进"隐藏导入 N 个"，对不上
        if mod in excludes:
            continue
        cmd += ['--hidden-import', mod]
    for mod in excludes:
        cmd += ['--exclude-module', mod]

    seen = set()
    for src, dest in cfg.get('datas', []):
        if not os.path.exists(src):
            continue
        key = (os.path.normcase(src), dest)
        if key in seen:
            continue
        seen.add(key)
        cmd += ['--add-data', f'{src}{os.pathsep}{dest}']

    cmd.append(entry)
    return cmd, notes


def _kill(proc):
    """连子进程一起杀。PyInstaller 会自己派生进程，只杀父进程会留下孤儿
    继续往 workpath 写文件，导致临时目录删不掉。"""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_build(cmd, log, workdir, cancel_flag):
    """流式跑 PyInstaller, 同时支持取消/超时"""
    log('[命令] ' + ' '.join(f'"{c}"' if ' ' in c else c for c in cmd))
    env = clean_env()
    try:
        proc = subprocess.Popen(
            cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', env=env,
            creationflags=NO_WINDOW)
    except FileNotFoundError as exc:
        return False, f'找不到解释器: {exc}'
    except Exception as exc:
        return False, str(exc)

    lines = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:
            pass
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    started = time.monotonic()
    try:
        while True:
            if cancel_flag and cancel_flag[0]:
                _kill(proc)
                return False, '用户取消'
            if time.monotonic() - started > BUILD_TIMEOUT:
                _kill(proc)
                return False, f'超过 {BUILD_TIMEOUT} 秒'
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            line = line.rstrip()
            if line:
                log(line)
        code = proc.wait()
        return code == 0, f'返回码 {code}'
    finally:
        if proc.poll() is None:
            _kill(proc)


def output_target(distpath, name, onefile):
    if onefile:
        return os.path.join(distpath, name + '.exe')
    return os.path.join(distpath, name, name + '.exe')


def locate_product(distpath, name, onefile, since=0):
    """
    找打包产物。.spec 模式下列表里的名字可能跟界面填的不一样,
    所以最后兜一层: 输出目录里最新的那个 exe / 文件夹。

    since: 本次构建开始的 unix 时间。兜底时只认「这次新写出来的文件」,
    否则会把别的项目/上一次的 exe 当成本次产物, 然后谎报成功。
    """
    if not distpath or not os.path.isdir(distpath):
        return None

    exact = output_target(distpath, name, onefile)
    if os.path.exists(exact):
        return exact

    for cand in (os.path.join(distpath, name + '.exe'),
                 os.path.join(distpath, name, name + '.exe')):
        if os.path.isfile(cand):
            return cand

    floor = (since - 2) if since else 0
    newest, newest_ts = None, -1
    try:
        for entry in os.listdir(distpath):
            full = os.path.join(distpath, entry)
            if os.path.isfile(full) and entry.lower().endswith('.exe'):
                ts = os.path.getmtime(full)
                if ts >= floor and ts > newest_ts:
                    newest, newest_ts = full, ts
            elif os.path.isdir(full):
                inner = os.path.join(full, entry + '.exe')
                if os.path.isfile(inner):
                    ts = os.path.getmtime(inner)
                    if ts >= floor and ts > newest_ts:
                        newest, newest_ts = inner, ts
    except OSError:
        pass
    return newest


def cleanup_temp(workpath, log, owned=True, product_name=''):
    """清理构建临时目录。

    owned=False 表示这个目录是用户自己在「高级 → 临时构建目录」里指定的，
    里面可能有别的东西 —— 绝不能整个删掉，只收拾本次构建生成的那几项。
    """
    if not workpath or not os.path.isdir(workpath):
        return

    if not owned:
        removed = []
        if product_name:
            for cand in (os.path.join(workpath, product_name),
                         os.path.join(workpath, product_name + '.spec')):
                try:
                    if os.path.isdir(cand):
                        shutil.rmtree(cand, ignore_errors=True)
                        removed.append(cand)
                    elif os.path.isfile(cand):
                        os.remove(cand)
                        removed.append(cand)
                except OSError:
                    pass
        if removed:
            log('[清理] 已删除本次构建的中间文件: '
                + ', '.join(os.path.basename(p) for p in removed))
        else:
            log(f'[清理] 保留你指定的构建目录: {workpath}')
        return

    try:
        shutil.rmtree(workpath, ignore_errors=True)
        log(f'[清理] 已删除构建临时目录: {workpath}')
    except Exception as exc:
        log(f'[警告] 清理临时目录失败: {exc}')


# ══════════════════════════════════════════════════════════════
#  对话框
# ══════════════════════════════════════════════════════════════
class CenterDialog(Toplevel):
    def __init__(self, master, title, width=720, height=520):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.resizable(True, True)
        self.result = None
        self.geometry(f'{width}x{height}')
        try:
            self.update_idletasks()
            px = master.winfo_rootx() + (master.winfo_width() - width) // 2
            py = master.winfo_rooty() + (master.winfo_height() - height) // 3
            self.geometry(f'+{max(px, 40)}+{max(py, 40)}')
        except Exception:
            pass

    def show(self):
        try:
            self.grab_set()
        except Exception:
            pass
        self.wait_window(self)
        return self.result


class EntryPicker(CenterDialog):
    """从目录里挑入口脚本"""

    def __init__(self, master, folder, files):
        super().__init__(master, '选择入口脚本', 640, 480)
        Label(self, text=f'目录: {folder}', anchor='w', fg='#555').pack(
            fill='x', padx=12, pady=(10, 4))
        Label(self, text='选中要作为程序入口的 .py 文件(双击也能确认)',
              anchor='w', fg='gray', font=('', 8)).pack(fill='x', padx=12)

        body = Frame(self)
        body.pack(fill='both', expand=True, padx=12, pady=8)
        self.lb = Listbox(body, selectmode=EXTENDED)
        self.lb.pack(side=LEFT, fill='both', expand=True)
        sb = Scrollbar(body, orient=VERTICAL, command=self.lb.yview)
        sb.pack(side=RIGHT, fill=Y)
        self.lb.config(yscrollcommand=sb.set)

        prio = {'main.py': 0, 'app.py': 1, 'run.py': 2, 'start.py': 3,
                '__main__.py': 4, 'gui.py': 5}
        rows = []
        for f in files:
            rel = os.path.relpath(f, folder)
            score = prio.get(os.path.basename(f).lower(), 10) + rel.count(os.sep)
            rows.append((score, rel, f))
        rows.sort()
        for _, rel, full in rows:
            self.lb.insert(END, rel)
        self._rows = [r[2] for r in rows]
        if self._rows:
            self.lb.selection_set(0)
        self.lb.bind('<Double-Button-1>', lambda e: self._ok())

        bar = Frame(self)
        bar.pack(fill='x', padx=12, pady=(0, 12))
        Button(bar, text='确定', width=12, command=self._ok,
               bg='#4a90d9', fg='white').pack(side=RIGHT, padx=4)
        Button(bar, text='取消', width=10, command=self.destroy).pack(side=RIGHT)

    def _ok(self):
        sel = self.lb.curselection()
        if not sel:
            messagebox.showwarning('提示', '先选一个入口脚本', parent=self)
            return
        self.result = self._rows[sel[0]]
        self.destroy()


class InterpreterPicker(CenterDialog):
    """扫描结果列表"""

    def __init__(self, master, items, current):
        super().__init__(master, '选择 Python 解释器', 820, 460)
        Label(self, text='本机扫描到的 Python(双击确认):',
              anchor='w', font=('', 9, 'bold')).pack(fill='x', padx=12, pady=(10, 4))

        cols = ('ver', 'bits', 'pyl', 'type', 'path')
        self.tree = ttk.Treeview(self, columns=cols, show='headings', height=12)
        for col, text, width in (
                ('ver', '版本', 70), ('bits', '位数', 50),
                ('pyl', 'PyInstaller', 100), ('type', '类型', 110),
                ('path', '路径', 460)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor='w')
        self.tree.pack(fill='both', expand=True, padx=12)
        sb = Scrollbar(self, orient=VERTICAL, command=self.tree.yview)
        sb.place(relx=1.0, rely=0.5, anchor='e')
        self.tree.config(yscrollcommand=sb.set)

        self.items = items
        for idx, info in enumerate(items):
            kind = '嵌入式(注意缺库)' if info['embedded'] else '标准安装'
            self.tree.insert('', END, iid=str(idx), values=(
                info['version'], info['bits'],
                info['pyinstaller'] or '未安装', kind, info['exe']))
            if os.path.normcase(info['exe']) == os.path.normcase(current or ''):
                self.tree.selection_set(str(idx))

        Label(self,
              text='提示: 嵌入式(embeddable) Python 默认没有 pip 和 site-packages, '
                   '打包前要确认它里面已经有你脚本用到的第三方库。',
              fg='#a15c00', anchor='w', wraplength=780, justify='left',
              font=('', 8)).pack(fill='x', padx=12, pady=(6, 0))

        self.tree.bind('<Double-Button-1>', lambda e: self._ok())
        bar = Frame(self)
        bar.pack(fill='x', padx=12, pady=10)
        Button(bar, text='使用这个解释器', width=16, command=self._ok,
               bg='#4a90d9', fg='white').pack(side=RIGHT, padx=4)
        Button(bar, text='取消', width=10, command=self.destroy).pack(side=RIGHT)

    def _ok(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning('提示', '先选一个解释器', parent=self)
            return
        self.result = self.items[int(sel[0])]
        self.destroy()


class ModuleSelectDialog(CenterDialog):
    """手动勾选要 hidden-import 的模块"""

    def __init__(self, master, rows, preselected):
        super().__init__(master, '手动选择要打包的模块', 760, 560)
        self.rows = rows          # [(name, kind, version, status)]
        Label(self, text='勾选需要强制打包进去的模块(第三方库建议全选):',
              anchor='w', font=('', 9, 'bold')).pack(fill='x', padx=12, pady=(10, 2))
        Label(self, text='红色「缺失」= 当前解释器里根本不存在这个库, '
                         '勾了也打不进去, 需要先 pip install 或用别的解释器。',
              anchor='w', fg='gray', font=('', 8)).pack(fill='x', padx=12, pady=(0, 6))

        body = Frame(self)
        body.pack(fill='both', expand=True, padx=12)
        self.lb = Listbox(body, selectmode=EXTENDED, font=('Consolas', 9))
        self.lb.pack(side=LEFT, fill='both', expand=True)
        sb = Scrollbar(body, orient=VERTICAL, command=self.lb.yview)
        sb.pack(side=RIGHT, fill=Y)
        self.lb.config(yscrollcommand=sb.set)

        self._names = []
        for idx, (name, kind, version, status) in enumerate(rows):
            self._names.append(name)
            text = f'{name:<34}{kind:<8}{version or "-":<14}{status}'
            self.lb.insert(END, text)
            if name in preselected:
                self.lb.selection_set(idx)

        bar = Frame(self)
        bar.pack(fill='x', padx=12, pady=(8, 2))
        Button(bar, text='全选', width=8,
               command=lambda: self.lb.selection_set(0, END)).pack(side=LEFT, padx=2)
        Button(bar, text='全不选', width=8,
               command=lambda: self.lb.selection_clear(0, END)).pack(side=LEFT, padx=2)
        Button(bar, text='反选', width=8,
               command=self._invert).pack(side=LEFT, padx=2)
        Button(bar, text='只选可用的', width=12,
               command=self._only_ok).pack(side=LEFT, padx=2)

        bar2 = Frame(self)
        bar2.pack(fill='x', padx=12, pady=(0, 12))
        Button(bar2, text='确定', width=12, command=self._ok,
               bg='#4a90d9', fg='white').pack(side=RIGHT, padx=4)
        Button(bar2, text='取消', width=10, command=self.destroy).pack(side=RIGHT)

    def _invert(self):
        chosen = set(self.lb.curselection())
        self.lb.selection_clear(0, END)
        for i in range(len(self._names)):
            if i not in chosen:
                self.lb.selection_set(i)

    def _only_ok(self):
        self.lb.selection_clear(0, END)
        for i, row in enumerate(self.rows):
            if row[3] != '缺失':
                self.lb.selection_set(i)

    def _ok(self):
        self.result = [self._names[i] for i in self.lb.curselection()]
        self.destroy()


class PolicyDialog(CenterDialog):
    """检测完自动弹出: 自动 / 手动 / 不打包"""

    def __init__(self, master, summary, missing):
        super().__init__(master, '模块检测完成', 640, 400)
        Label(self, text='模块检测完成', font=('', 12, 'bold')).pack(
            anchor='w', padx=16, pady=(14, 6))
        Label(self, text=summary, anchor='w', justify='left',
              wraplength=600).pack(fill='x', padx=16)

        if missing:
            Label(self, text='⚠ 以下模块在当前解释器里找不到, 打进 exe 后运行会崩:',
                  fg='#c0392b', anchor='w', font=('', 9, 'bold')).pack(
                fill='x', padx=16, pady=(10, 2))
            mtxt = Text(self, height=min(5, len(missing) + 1), wrap=WORD,
                        bg='#fff5f5', fg='#c0392b', relief='flat')
            mtxt.pack(fill='x', padx=16)
            mtxt.insert('1.0', ', '.join(missing))
            mtxt.config(state=DISABLED)

        box = Frame(self)
        box.pack(fill='both', expand=True, padx=16, pady=10)

        Button(box, text='① 自动 — 把检测到的模块全部打包进去(推荐)',
               anchor='w', command=lambda: self._pick('auto')
               ).pack(fill='x', pady=3)
        Button(box, text='② 手动 — 列出模块清单, 我自己勾',
               anchor='w', command=lambda: self._pick('manual')
               ).pack(fill='x', pady=3)
        Button(box, text='③ 不打包模块 — 只靠 PyInstaller 自己分析',
               anchor='w', command=lambda: self._pick('none')
               ).pack(fill='x', pady=3)
        Label(box, text='选 ③ 的话不会额外强制打入任何模块。除了本机装了同样的库, '
                        '换一台电脑极大概率无法运行(报 ModuleNotFoundError / 双击闪退)。',
              fg='#c0392b', font=('', 8), wraplength=580,
              justify='left', anchor='w').pack(fill='x', pady=(2, 0))

    def _pick(self, value):
        self.result = value
        self.destroy()


# ══════════════════════════════════════════════════════════════
#  主界面
# ══════════════════════════════════════════════════════════════
class PackerApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry('980x930')
        root.minsize(900, 760)

        self.build_log = []
        self.last_success_output = None
        self.last_dist_dir = None
        self._cancel_flag = [False]
        self._log_queue = queue.Queue()
        self._building = False
        self._build_result = None
        self._detect_result = None
        self._detect_running = False
        self._detect_pending = None
        self._interp_list = []
        self._scan_running = False
        self._interp_ready = False
        self._startup_result = None

        self.detect = self._empty_detect()

        # ── 变量 ──
        self.entry_script = StringVar()
        self.project_dir = StringVar()
        self.python_exe = StringVar(value=sys.executable)
        self.name_var = StringVar()
        self.icon_var = StringVar()
        self.output_dir_var = StringVar()
        self.policy_var = StringVar(value='auto')
        self.debug_var = StringVar(value='')
        self.exclude_var = StringVar()
        self.version_file_var = StringVar()
        self.splash_var = StringVar()
        self.upx_dir_var = StringVar()
        self.workpath_var = StringVar()
        self.runtime_tmpdir_var = StringVar()

        self.mode_var = IntVar(value=0)
        self.onefile_var = BooleanVar(value=True)
        self.no_console_var = BooleanVar(value=False)
        self.disable_traceback_var = BooleanVar(value=False)
        self.uac_admin_var = BooleanVar(value=False)
        self.uac_uiaccess_var = BooleanVar(value=False)
        self.clean_var = BooleanVar(value=True)
        self.hook_var = BooleanVar(value=True)
        self.test_run_var = BooleanVar(value=True)
        self.keep_temp_var = BooleanVar(value=False)

        self.debug_labels = {
            '': '关闭 (正常打包)',
            'all': '全部: 所有调试信息',
            'imports': '导入: 查找模块过程',
            'bootloader': '引导: exe 启动引导过程',
            'noarchive': '解包: 不压缩, 看文件结构',
        }

        # ── 轻量模式（外挂模块，可能不存在）──
        self.light_version = StringVar(value='1.0.0.0')
        self.light_module, self.light_msg = load_light_module()
        self.light_ok, self.light_status = False, self.light_msg
        if self.light_module is not None:
            try:
                self.light_ok, self.light_status = self.light_module.available()
            except Exception as exc:
                self.light_ok, self.light_status = False, f'检测失败: {exc}'

        self._build_menu()
        self._build_notebook()
        self._build_log_area()
        self._build_bottom_bar()

        self._load_config()
        self._refresh_mode_ui()
        self._refresh_module_summary()

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.after(150, self._startup_check)
        self.root.after(120, self._heartbeat)

        if self.entry_script.get().strip():
            self.root.after(600, lambda: self._start_detect(show_dialog=False))

    # ── 小工具 ──
    def _empty_detect(self):
        return {'files': [], 'third': {}, 'stdlib': {}, 'local': {},
                'missing': {}, 'selected': set(), 'errors': [],
                'scope': '', 'done': False}

    def _ui_section(self, parent, title, hint=''):
        head = Frame(parent)
        head.pack(fill='x', padx=12, pady=(7, 1))
        Label(head, text=title, font=('', 10, 'bold'), fg='#2c5d8f').pack(side=LEFT)
        if hint:
            Label(head, text=hint, fg='gray', font=('', 8)).pack(side=LEFT, padx=8)
        body = Frame(parent)
        body.pack(fill='x', padx=22, pady=(0, 1))
        return body

    def _log(self, text):
        self.build_log.append(text)
        try:
            self.log_text.config(state=NORMAL)
            cur = int(self.log_text.index('end-1c').split('.')[0])
            if cur > BUILD_LOG_LINES:
                self.log_text.delete('1.0', f'{cur - BUILD_LOG_LINES}.0')
            self.log_text.insert(END, text + '\n')
            self.log_text.see(END)
            self.log_text.config(state=DISABLED)
        except Exception:
            pass

    def _queue_log(self, text):
        self._log_queue.put(text)

    def _drain_logs(self):
        for _ in range(300):
            try:
                text = self._log_queue.get_nowait()
            except queue.Empty:
                return
            self._log(text)

    def _heartbeat(self):
        # 整段兜住: 之前只有最后那次 after 有 try, 中间任何一处抛异常
        # 心跳链就永久断开 → 日志不刷、构建结果没人取、_building 永远 True、
        # 窗口也关不掉, 只能任务管理器结束进程。
        try:
            self._drain_logs()

            if getattr(self, '_interp_ready', False):
                self._interp_ready = False
                self._show_interp_list()

            if self._detect_result is not None:
                payload = self._detect_result
                self._detect_result = None
                if payload == 'EMPTY':
                    self._detect_done_ui()
                    self.detect = self._empty_detect()
                    self._refresh_module_summary()
                    self._fill_mod_tree()
                    self._resume_pending_detect()
                else:
                    self._apply_detection(*payload)

            if self._build_result is not None:
                payload = self._build_result
                self._build_result = None
                self._finish_build(*payload)
        except Exception:
            try:
                self._log('[异常] 心跳线程出错: ' + traceback.format_exc(limit=3))
            except Exception:
                pass
        finally:
            try:
                self.root.after(120, self._heartbeat)
            except Exception:
                pass

    # ── 菜单 ──
    def _build_menu(self):
        menubar = Menu(self.root)
        help_menu = Menu(menubar, tearoff=0)
        help_menu.add_command(label='术语速查', command=self._show_glossary)
        help_menu.add_command(label='常见故障(启动崩溃对照表)', command=self._show_troubleshoot)
        help_menu.add_command(label='关于', command=self._show_about)
        menubar.add_cascade(label='帮助', menu=help_menu)
        self.root.config(menu=menubar)

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=8, pady=(6, 0))

        self.tab_home = Frame(self.notebook)
        self.notebook.add(self.tab_home, text='  主页  ')
        self._build_tab_home()

        self.tab_modules = Frame(self.notebook)
        self.notebook.add(self.tab_modules, text='  模块  ')
        self._build_tab_modules()

        self.tab_data = Frame(self.notebook)
        self.notebook.add(self.tab_data, text='  数据文件  ')
        self._build_tab_data()

        self.tab_advanced = Frame(self.notebook)
        self.notebook.add(self.tab_advanced, text='  高级  ')
        self._build_tab_advanced()

        self.tab_help = Frame(self.notebook)
        self.notebook.add(self.tab_help, text='  讲解  ')
        self._build_tab_help()

    # ── 主页 ──
    def _build_tab_home(self):
        outer = Frame(self.tab_home)
        outer.pack(fill='both', expand=True)

        self.home_canvas = Canvas(outer, highlightthickness=0)
        vsb = Scrollbar(outer, orient=VERTICAL, command=self.home_canvas.yview)
        vsb.pack(side=RIGHT, fill=Y)
        self.home_canvas.pack(side=LEFT, fill='both', expand=True)
        self.home_canvas.configure(yscrollcommand=vsb.set)

        inner = Frame(self.home_canvas)
        win = self.home_canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda e: self.home_canvas.configure(scrollregion=self.home_canvas.bbox('all')))
        self.home_canvas.bind('<Configure>',
                             lambda e: self.home_canvas.itemconfigure(win, width=e.width))

        def _wheel(event):
            try:
                self.home_canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            except Exception:
                pass

        self.home_canvas.bind('<Enter>',
                              lambda e: self.home_canvas.bind_all('<MouseWheel>', _wheel))
        self.home_canvas.bind('<Leave>',
                              lambda e: self.home_canvas.unbind_all('<MouseWheel>'))

        # ① 要打包什么
        b = self._ui_section(inner, '① 要打包什么', '(选完自动开始检测模块)')
        row = Frame(b)
        row.pack(fill='x', pady=3)
        Label(row, text='入口脚本', width=9, anchor='e').pack(side=LEFT, padx=(0, 4))
        Entry(row, textvariable=self.entry_script).pack(side=LEFT, fill='x', expand=True)
        Button(row, text='选脚本…', command=self._browse_entry_file,
               width=9).pack(side=LEFT, padx=3)
        self.dir_btn = Button(row, text='选目录…', command=self._browse_project_dir,
                              width=9)
        self.dir_btn.pack(side=LEFT)
        Button(row, text='×', command=self._clear_entry, width=2).pack(side=LEFT, padx=3)

        row2 = Frame(b)
        row2.pack(fill='x', pady=3)
        Label(row2, text='项目目录', width=9, anchor='e').pack(side=LEFT, padx=(0, 4))
        self.project_entry = Entry(row2, textvariable=self.project_dir)
        self.project_entry.pack(side=LEFT, fill='x', expand=True)
        self.project_btn = Button(row2, text='浏览…',
                                  command=self._browse_project_dir_only, width=9)
        self.project_btn.pack(side=LEFT, padx=3)
        self.project_hint = Label(
            b, text='目录模式下: 这个目录里的所有资源/模块会被打进 exe; 留空则用入口脚本所在的目录',
            fg='gray', font=('', 8), anchor='w')
        self.project_hint.pack(fill='x')

        # ② 模式
        b = self._ui_section(inner, '② 打包模式')
        modes = [
            (0, '模式1  单文件   — 一个入口 .py(+指定的数据文件) 压成单个 .exe'),
            (1, '模式2  目录模式 — 选整个项目目录 + 入口脚本 → 输出单个 exe, 双击直接跑入口, 项目依赖全在里面'),
            (2, '模式3  文件夹模式 — onedir, 输出一个文件夹(exe+依赖), 启动最快'),
            (3, '模式4  .spec 施工图 — 直接用已有的 .spec 文件打包'),
            (4, '模式5  轻量启动器 — 不打包 Python, 只包成十几 KB 的 exe (目标机必须已装 Python)'),
        ]
        self.mode_buttons = {}
        for idx, label in modes:
            rb = Radiobutton(b, text=label, variable=self.mode_var, value=idx,
                             anchor='w', command=self._on_mode_change)
            rb.pack(fill='x', pady=0)
            self.mode_buttons[idx] = rb
        self.light_note = Label(
            b, text='模式5 说明: 生成的 exe 只有十几 KB，双击就执行你的脚本，没有黑窗口。'
                    '代价是目标电脑要自己装 Python 和脚本用到的库；目录模式在这条路里不适用(会太大)。',
            fg='#8a6d00', font=('', 8), anchor='w', justify='left', wraplength=900)

        # ③ 解释器
        b = self._ui_section(inner, '③ Python 解释器', '(打包和模块检测都用它)')
        row = Frame(b)
        row.pack(fill='x', pady=3)
        Label(row, text='解释器', width=9, anchor='e').pack(side=LEFT, padx=(0, 4))
        self.interp_combo = ttk.Combobox(row, textvariable=self.python_exe)
        self.interp_combo.pack(side=LEFT, fill='x', expand=True)
        Button(row, text='扫描本机', command=self._scan_interp,
               width=9).pack(side=LEFT, padx=3)
        Button(row, text='浏览…', command=self._browse_interp,
               width=7).pack(side=LEFT)
        Button(row, text='装 PyInstaller', command=self._install_pyinstaller,
               width=13).pack(side=LEFT, padx=3)

        self.interp_hint = Label(
            b,
            text='提示: 如果选择了嵌入式(embeddable) Python，请注意它是否有对应的库可以打包你要打包的文件'
                 '(嵌入式版本默认没有 pip / site-packages，缺库会导致打包失败或 exe 启动即崩)。',
            fg='#8a6d00', font=('', 8), anchor='w', justify='left',
            wraplength=780)
        self.interp_hint.pack(fill='x', pady=(2, 0))

        # ④ 输出
        b = self._ui_section(inner, '④ 输出')
        row = Frame(b)
        row.pack(fill='x', pady=2)
        Label(row, text='输出名', width=9, anchor='e').pack(side=LEFT, padx=(0, 4))
        Entry(row, textvariable=self.name_var, width=30).pack(side=LEFT)
        Label(row, text='(留空 = 用入口文件名)', fg='gray',
              font=('', 8)).pack(side=LEFT, padx=6)

        row = Frame(b)
        row.pack(fill='x', pady=2)
        Label(row, text='图标', width=9, anchor='e').pack(side=LEFT, padx=(0, 4))
        Entry(row, textvariable=self.icon_var).pack(side=LEFT, fill='x', expand=True)
        Button(row, text='选择…', command=self._browse_icon,
               width=7).pack(side=LEFT, padx=3)
        Button(row, text='×', command=lambda: self.icon_var.set(''),
               width=2).pack(side=LEFT)

        row = Frame(b)
        row.pack(fill='x', pady=2)
        Label(row, text='输出目录', width=9, anchor='e').pack(side=LEFT, padx=(0, 4))
        Entry(row, textvariable=self.output_dir_var).pack(side=LEFT, fill='x', expand=True)
        Button(row, text='选择…', command=self._browse_output_dir,
               width=7).pack(side=LEFT, padx=3)
        Button(row, text='打开', command=self._open_output, width=6).pack(side=LEFT)
        Label(b, text='留空 = 入口脚本所在目录下的 dist\\ 文件夹；打包产生的中间文件在 '
                      'LOCALAPPDATA\\PyPacker，打完自动清理，不落在桌面',
              fg='gray', font=('', 8), anchor='w').pack(fill='x')

        # ⑤ 打包参数 —— 两套面板，按模式自动切换
        b = self._ui_section(inner, '⑤ 打包参数')

        # ── PyInstaller 面板（模式 1~4）──
        self.pane_pyi = Frame(b)
        grid = Frame(self.pane_pyi)
        grid.pack(fill='x')
        checks = [
            ('onefile', '单文件 (--onefile)', self.onefile_var, self._on_onefile_toggle),
            ('console', '隐藏控制台黑窗 (--noconsole)', self.no_console_var, None),
            ('uac', '请求管理员权限 (--uac-admin)', self.uac_admin_var, None),
            ('clean', '清理缓存（删掉上次的打包中间文件，防止旧缓存干扰；会慢一点）',
             self.clean_var, None),
            ('hook', '运行时切到资源目录（open("配置.json") 这类相对路径才能找到项目文件）',
             self.hook_var, None),
            ('test', '打包后试运行 5 秒自检（自动跑一下产物，秒退就说明缺模块或路径不对）',
             self.test_run_var, None),
            ('notb', '崩溃时不弹错误框 (--disable-windowed-traceback)',
             self.disable_traceback_var, None),
        ]
        self.chk = {}
        for i, (key, text, var, cmd) in enumerate(checks):
            w = Checkbutton(grid, text=text, variable=var, command=cmd, anchor='w')
            w.grid(row=i // 2, column=i % 2, sticky='w', padx=(0, 24), pady=0)
            self.chk[key] = w
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        # ── 轻量模式面板（模式 5）──
        # 不打包 Python，所以 PyInstaller 那一整套参数都没有对应概念；
        # 只留真正会写进 exe 的两项。
        self.pane_light = Frame(b)
        lgrid = Frame(self.pane_light)
        lgrid.pack(fill='x')
        self.chk_light = {}
        for i, (key, text, var) in enumerate((
                ('console', '隐藏控制台黑窗（用 pythonw 跑, 不弹命令行）', self.no_console_var),
                ('uac', '请求管理员权限（UAC 提权）', self.uac_admin_var),
                ('test', '打包后试运行 5 秒自检', self.test_run_var))):
            w = Checkbutton(lgrid, text=text, variable=var, anchor='w')
            w.grid(row=0, column=i, sticky='w', padx=(0, 22), pady=0)
            self.chk_light[key] = w
        Label(self.pane_light,
              text='轻量模式没有 Python 运行时，所以单文件 / 隐藏导入 / UPX / 清理缓存 / 缓冲目录这些参数不存在，'
                   '这里只留会真正写进 exe 的选项。名称、图标、输出目录在 ④ 区照常生效。',
              fg='gray', font=('', 8), anchor='w', justify='left',
              wraplength=800).pack(fill='x', pady=(2, 0))
        self.pane_pyi.pack(fill='x')

        # ⑥ 模块
        b = self._ui_section(inner, '⑥ 模块检测', '(选完文件会自动检测)')
        self.module_status = Label(b, text='尚未检测', anchor='w', fg='#555',
                                   justify='left', wraplength=780)
        self.module_status.pack(fill='x', pady=(2, 3))

        row = Frame(b)
        row.pack(fill='x', pady=2)
        self.policy_radios = {}
        for key, text in (('auto', '自动(全部打入)'), ('manual', '手动选择'),
                          ('none', '不打包模块')):
            rb = Radiobutton(row, text=text, variable=self.policy_var,
                             value=key, command=self._on_policy_change)
            rb.pack(side=LEFT, padx=(0, 10))
            self.policy_radios[key] = rb
        Button(row, text='重新检测', command=lambda: self._start_detect(True),
               width=9).pack(side=LEFT, padx=4)
        self.picker_btn = Button(row, text='查看/勾选模块',
                                 command=self._open_module_picker, width=13)
        self.picker_btn.pack(side=LEFT)

        self.policy_warn = Label(
            b, text='⚠ 已选择「不打包模块」: 不会强制打入任何第三方模块。'
                    '除了目标电脑上恰好装了同样的库，否则极大概率无法运行(双击闪退 / ModuleNotFoundError)。',
            fg='#c0392b', font=('', 8), anchor='w', justify='left', wraplength=780)

        self.home_status = Label(inner, text='', fg='gray', anchor='w')
        self.home_status.pack(fill='x', padx=12, pady=(4, 6))

    # ── 模块页 ──
    def _build_tab_modules(self):
        pad = {'padx': 10, 'pady': 4}
        Label(self.tab_modules,
              text='检测结果明细(只读)。要改「打入哪些」，请回主页点「查看/勾选模块」。',
              font=('', 9, 'bold'), anchor='w').pack(fill='x', **pad)

        cols = ('name', 'kind', 'version', 'status', 'path')
        self.mod_tree = ttk.Treeview(self.tab_modules, columns=cols,
                                     show='headings', height=14)
        for col, text, width in (('name', '模块', 260), ('kind', '类型', 90),
                                 ('version', '版本', 100), ('status', '状态', 80),
                                 ('path', '位置', 380)):
            self.mod_tree.heading(col, text=text)
            self.mod_tree.column(col, width=width, anchor='w')
        sb = Scrollbar(self.tab_modules, orient=VERTICAL, command=self.mod_tree.yview)
        self.mod_tree.pack(side=LEFT, fill='both', expand=True, padx=(10, 0), pady=4)
        sb.pack(side=RIGHT, fill=Y, padx=(0, 10), pady=4)
        self.mod_tree.config(yscrollcommand=sb.set)
        self.mod_tree.tag_configure('missing', foreground='#c0392b')
        self.mod_tree.tag_configure('local', foreground='#7f8c8d')
        self.mod_tree.tag_configure('std', foreground='#7f8c8d')

    # ── 数据文件 ──
    def _build_tab_data(self):
        pad = {'padx': 10, 'pady': 4}
        Label(self.tab_data,
              text='额外数据文件(图片/音乐/json/dll…)。目录模式会自动打包整个项目目录，一般不用在这加。',
              font=('', 9, 'bold'), anchor='w').pack(fill='x', **pad)

        f2 = Frame(self.tab_data)
        f2.pack(fill='both', expand=True, **pad)
        self.data_files = []
        self.data_listbox = Listbox(f2, height=10, selectmode=EXTENDED)
        self.data_listbox.pack(side=LEFT, fill='both', expand=True)
        sb = Scrollbar(f2, orient=VERTICAL, command=self.data_listbox.yview)
        sb.pack(side=RIGHT, fill=Y)
        self.data_listbox.config(yscrollcommand=sb.set)

        f2b = Frame(self.tab_data)
        f2b.pack(fill='x', **pad)
        Button(f2b, text='+ 添加文件', command=self._add_data_file).pack(side=LEFT, padx=3)
        Button(f2b, text='+ 添加目录', command=self._add_data_dir).pack(side=LEFT, padx=3)
        Button(f2b, text='- 移除选中', command=self._remove_data_file).pack(side=LEFT, padx=3)
        Button(f2b, text='清空全部', command=self._clear_data_files).pack(side=LEFT, padx=3)

        Label(self.tab_data,
              text='说明: 数据文件会被塞进 exe, 运行时解压到临时目录。\n'
                   '代码里用 sys._MEIPASS 或相对路径访问(勾了「运行时切到资源目录」就不用管)。\n'
                   '目标目录统一是 exe 内部根目录, 保持原来的相对路径。',
              fg='gray', wraplength=680, justify='left').pack(anchor='w', **pad)

    # ── 高级 ──
    def _build_tab_advanced(self):
        pad = {'padx': 10, 'pady': 3}
        Label(self.tab_advanced, text='高级选项(留空=默认值)',
              font=('', 9, 'bold')).pack(anchor='w', padx=10, pady=(8, 3))

        rows = [
            ('版本信息文件', self.version_file_var, self._browse_version,
             '右键exe→属性→详细信息', self._generate_version_template, '生成模板'),
            ('启动画面(.png)', self.splash_var, self._browse_splash, '实验性', None, None),
            ('UPX 目录', self.upx_dir_var, self._browse_upx_dir,
             '（用 UPX 再把 exe 压一遍，体积更小；不填=不压缩）', None, None),
            ('临时构建目录', self.workpath_var, self._browse_workpath,
             '（打包中间文件放哪；留空=LOCALAPPDATA\\PyPacker，打完自动删）', None, None),
            ('运行时解压目录', self.runtime_tmpdir_var, self._browse_runtime_tmpdir,
             '（单文件运行时解压到哪；留空=系统临时目录，退出即删）', None, None),
        ]
        for label, var, browse_cmd, tip, gen_cmd, gen_label in rows:
            row_frame = Frame(self.tab_advanced)
            row_frame.pack(fill='x', **pad)
            Label(row_frame, text=label, width=14, anchor='e').pack(side=LEFT, padx=(0, 4))
            Entry(row_frame, textvariable=var, width=40).pack(side=LEFT, fill='x', expand=True)
            Button(row_frame, text='..', command=browse_cmd, width=3).pack(side=LEFT, padx=3)
            if gen_cmd:
                Button(row_frame, text=gen_label, command=gen_cmd,
                       width=8).pack(side=LEFT, padx=2)
            Label(row_frame, text=tip, fg='gray', font=('', 8)).pack(side=LEFT, padx=6)

        exc_frame = Frame(self.tab_advanced)
        exc_frame.pack(fill='x', **pad)
        Label(exc_frame, text='排除模块', width=14, anchor='e').pack(side=LEFT, padx=(0, 4))
        Entry(exc_frame, textvariable=self.exclude_var, width=40).pack(
            side=LEFT, fill='x', expand=True)
        Label(exc_frame, text='（强制不打进 exe 的库，用来瘦身；逗号分隔，如 tkinter,numpy）',
              fg='gray', font=('', 8)).pack(side=LEFT, padx=6)

        dbg = Frame(self.tab_advanced)
        dbg.pack(fill='x', **pad)
        Label(dbg, text='调试级别', width=14, anchor='e').pack(side=LEFT, padx=(0, 4))
        for val, lab in self.debug_labels.items():
            Radiobutton(dbg, text=lab, variable=self.debug_var, value=val,
                        command=self._refresh_module_summary).pack(side=LEFT, padx=6)

        misc = Frame(self.tab_advanced)
        misc.pack(fill='x', **pad)
        Checkbutton(misc, text='保留构建临时文件(排查用)',
                    variable=self.keep_temp_var).pack(side=LEFT, padx=(90, 20))

        warn = Frame(self.tab_advanced)
        warn.pack(fill='x', pady=(14, 2), padx=10)
        Label(warn, text='ℹ 关于 --uac-uiaccess（本工具不提供）',
              fg='#8a6d3b', font=('', 9, 'bold'), anchor='w').pack(fill='x')
        Label(warn,
              text='它会把 exe 清单写成 uiAccess="true"。Windows 只允许「已签名 且 安装在 '
                   'Program Files 之类受保护目录」的程序使用，否则双击直接报\n'
                   '「从服务器返回了一个参照」而根本起不来 —— 你之前那个打不开的 exe 就是它造成的。\n'
                   '普通程序完全不需要它，所以这个勾选框已经删掉了。',
              fg='#8a6d3b', justify='left', anchor='w',
              font=('', 8), wraplength=760).pack(fill='x', padx=20, pady=(2, 10))

    # ── 讲解 ──
    def _build_tab_help(self):
        help_text = (
            '═════════ 使用顺序 ═════════\n\n'
            '  ① 选入口脚本(或直接选整个项目目录)\n'
            '  ② 选打包模式\n'
            '  ③ 确认解释器(默认就是启动本工具的那个 Python)\n'
            '  ④ 确认输出目录\n'
            '  ⑤ 模块检测弹窗里选「自动」\n'
            '  ⑥ 点「开始打包」\n\n'
            '═════════ 关于「从服务器返回了一个参照」═════════\n\n'
            '  这个报错跟 PyInstaller、跟你的代码都没关系，是 Windows 拒绝启动这个 exe。\n'
            '  唯一原因: exe 的清单里写了 uiAccess="true"(--uac-uiaccess 造成的)。\n'
            '  Windows 规定 uiAccess 只给「已数字签名 + 装在 Program Files 等受保护目录」\n'
            '  的辅助功能程序用。你自己的 exe 没签名、还放在桌面 → 系统直接拒绝 → 这个报错。\n\n'
            '  解决办法: 关掉「高级」里的 --uac-uiaccess，重新打包即可。\n'
            '  需要管理员权限的话，只勾 --uac-admin 就够了(启动弹 UAC 是正常的)。\n\n'
            '═════════ 模块检测是怎么做的 ═════════\n\n'
            '  不是搜字符串(那样注释里出现 numpy 也会被当成依赖，exe 会白胖几十 MB)。\n'
            '  现在是把源码解析成语法树，只认真正的导入语句:\n'
            '      import a.b.c\n'
            '      from x import y, z          → 同时记 x 和 x.y / x.z\n'
            '      from . import sibling       → 项目内部导入，不算依赖\n'
            '      __import__("numpy")\n'
            '      importlib.import_module("pandas")\n'
            '      importlib.util.find_spec("cv2")\n\n'
            '  然后拿「你选中的那个解释器」逐个查 find_spec:\n'
            '      能查到 → 第三方，可以强制打进 exe\n'
            '      查不到 → 标红「缺失」，这库根本没装，打进去也没用\n'
            '      标准库 / 项目自己的模块 → 不重复打\n\n'
            '  三种策略:\n'
            '    自动  — 把检测到的全部 --hidden-import 进去。最稳。\n'
            '    手动  — 弹出清单自己勾。想瘦身/排除干扰时用。\n'
            '    不打包 — 只靠 PyInstaller 的自动分析。本机肯定能跑，\n'
            '             换台电脑极大概率 ModuleNotFoundError / 双击闪退。\n\n'
            '═════════ 目录模式到底是什么 ═════════\n\n'
            '  你选「整个项目目录 + 入口脚本」，工具会:\n'
            '    · 把项目目录里除了 __pycache__/build/dist/venv/.git 之外的所有东西\n'
            '      按原相对路径 --add-data 进 exe\n'
            '    · 入口脚本作为程序入口\n'
            '    · 生成一个 exe, 双击就等于运行入口脚本, 而它读到的图片/配置/\n'
            '      子模块全都还在(在 exe 内部的资源目录里)\n'
            '    · 顺手加一个运行时钩子把工作目录切到资源目录，\n'
            '      这样源码里的 open("config.json") 这类相对路径不会失效\n\n'
            '  「文件夹模式(onedir)」是另一个东西: 输出一个文件夹, exe 和依赖并排，\n'
            '  启动快、方便换配置，但要整个文件夹一起拷走。\n\n'
            '═════════ 嵌入式(embeddable) Python ═════════\n\n'
            '  官方那个 python-3.x.x-embed-amd64.zip 解出来的就是。特点:\n'
            '    · 没有 pip、没有 Lib\\site-packages\n'
            '    · python3xx._pth 里默认连当前目录导入都关了\n'
            '    · 体积小，适合贴到别人机器上跑\n\n'
            '  拿它打包的前提: 它里面必须已经有你脚本要用的第三方库(requests、\n'
            '  numpy 之类)。没有的话，PyInstaller 分析不到 → 打出来的 exe 一启动就崩。\n'
            '  检测结果里那些标红的「缺失」就是这种情况。\n\n'
            '  稳妥做法: 用标准安装的 Python(带 pip 的那种)打包。\n\n'
            '═════════ 打包后启动失败的对照表 ═════════\n\n'
            '  双击完全没反应/一闪就没\n'
            '      → 先别勾 --noconsole, 用控制台模式重打一次, 看报什么错\n'
            '      → 或者「帮助 → 常见故障」看对照表\n'
            '  ModuleNotFoundError: No module named xxx\n'
            '      → 模块没打进包。回主页重新检测, 选「自动」, 确认解释器里确实装了这个库\n'
            '  FileNotFoundError: 某个图片/json\n'
            '      → 资源没打进去。用目录模式, 或勾上「运行时切到资源目录」\n'
            '  Failed to execute script xxx\n'
            '      → 入口脚本本身有语法/导入错误, 先在命令行 python 跑一遍\n'
            '  从服务器返回了一个参照\n'
            '      → 见上面第一条, 关掉 --uac-uiaccess\n'
            '  杀毒软件报毒\n'
            '      → onefile 自解压容易被误判, 换 onedir 或加白名单\n\n'
            '═════════ 模式5：轻量启动器（不打包 Python）═════════\n\n'
            '  这条路完全不用 PyInstaller。它用 Windows 自带的 C# 编译器\n'
            '  (csc.exe，跟着 .NET Framework 4.x 一起装在系统里) 现场编一个\n'
            '  十几 KB 的引导器，把你的 .py 当资源嵌进去。\n\n'
            '  体积:  PyInstaller 单文件 34MB  →  轻量模式十几 KB\n'
            '  代价:  目标电脑必须自己装 Python 3 和脚本用到的库\n\n'
            '  双击之后它做四件事:\n'
            '    1. 把内嵌的脚本和数据解到 %LOCALAPPDATA%\\<名字>_lite\\<内容hash>\\\n'
            '    2. 按顺序找本机的 Python:\n'
            '         PYPACKER_PYTHON 环境变量 → 构建时用的解释器 → 注册表\n'
            '         → PATH → 常见安装目录\n'
            '    3. 用 pythonw.exe 无窗运行你的脚本(控制台模式则用 python.exe)\n'
            '    4. 找不到 Python → 弹窗告诉你装 Python\n'
            '       脚本报 No module named xxx → 弹窗直接给出 pip install xxx\n\n'
            '  名字: 输出名留空会自动变成「脚本名-需PY.exe」，一眼看出这是要环境的版本；\n'
            '        自己填了名字就用你填的。\n\n'
            '  这条路用不上的参数(界面上会自动置灰):\n'
            '        --onefile / --onedir、--hidden-import、--exclude-module、\n'
            '        --upx-dir、--splash、--debug、--clean\n'
            '        原因: 这里没有 Python 可以打，模块由目标机的 Python 自己解析。\n\n'
            '  目录模式不兼容: 整个目录塞进去体积就回到 MB 级，跟「省空间」冲突。\n'
            '  要带整个项目请用模式2。\n\n'
            '  什么情况下别用它:\n'
            '        · 要发给完全不懂电脑的人 → 用模式1，大但不会缺库\n'
            '        · 目标机是 Win7 又没装 .NET 4 → 引导器起不来\n'
            '        · 杀软敏感 → 自己编的小 exe 偶尔被误报，加白名单即可\n\n'
            '  依赖关系: 这个功能在独立的 light_stub.py 里。把那个文件删掉，\n'
            '  本程序照常启动，只是模式5 会显示「不可用」。\n\n'
            '═════════ 其他参数速查 ═════════\n\n'
            '  --onefile / -F    打成一个 exe\n'
            '  --onedir  / -D    打成一个文件夹\n'
            '  --noconsole / -w  GUI 程序不弹黑窗\n'
            '  --add-data        把文件/目录塞进 exe\n'
            '  --hidden-import   强制打包某个模块\n'
            '  --exclude-module  排除某个模块(瘦身)\n'
            '  --clean           清掉上次的缓存\n'
            '  --uac-admin       要管理员权限\n'
            '  --debug all       全量调试日志\n'
            '  .spec             施工图纸, 所有命令行参数最终都翻译成它\n'
        )

        frame = Frame(self.tab_help)
        frame.pack(fill='both', expand=True, padx=8, pady=4)
        widget = Text(frame, wrap=WORD, font=('微软雅黑', 10), bg='#f7f7f7',
                      fg='#222', borderwidth=0, padx=12, pady=10)
        widget.pack(side=LEFT, fill='both', expand=True)
        sb = Scrollbar(frame, orient=VERTICAL, command=widget.yview)
        sb.pack(side=RIGHT, fill=Y)
        widget.config(yscrollcommand=sb.set)
        widget.insert('1.0', help_text)
        widget.config(state=DISABLED)

    # ── 日志 / 底栏 ──
    def _build_log_area(self):
        log_frame = Frame(self.root, height=104)
        log_frame.pack(fill='x', expand=False, padx=8, pady=(2, 2))
        log_frame.pack_propagate(False)
        Label(log_frame, text='▼ 构建日志', font=('', 8)).pack(anchor='w', padx=2)
        self.log_text = Text(log_frame, wrap=WORD, state=DISABLED, bg='#1e1e1e',
                             fg='#d4d4d4', insertbackground='white',
                             font=('Consolas', 9), height=5)
        self.log_text.pack(fill='both', expand=True, padx=2, pady=(0, 2))

    def _build_bottom_bar(self):
        self.progress_frame = Frame(self.root)
        self.progress_frame.pack(fill='x', padx=8, pady=(0, 2))
        self.progress_bar = ttk.Progressbar(self.progress_frame, mode='indeterminate',
                                            length=400)
        self.progress_status = Label(self.progress_frame, text='', fg='gray',
                                     font=('', 8))

        bar = Frame(self.root)
        bar.pack(fill='x', padx=8, pady=(0, 6))

        self.btn_build = Button(bar, text='▶  开 始 打 包', command=self._start_build,
                                bg='#4a90d9', fg='white',
                                font=('微软雅黑', 10, 'bold'), width=16)
        self.btn_build.pack(side=LEFT, padx=4)

        self.btn_cancel = Button(bar, text='■  取消打包', command=self._cancel_build,
                                 bg='#d94a4a', fg='white',
                                 font=('微软雅黑', 10, 'bold'), width=12)

        Button(bar, text='打开输出目录', command=self._open_output,
               width=12).pack(side=LEFT, padx=2)
        Button(bar, text='试运行上次产物', command=self._test_last,
               width=14).pack(side=LEFT, padx=2)
        Button(bar, text='清空日志', command=self._clear_log, width=9).pack(side=LEFT, padx=2)
        Button(bar, text='复制日志', command=self._copy_log, width=9).pack(side=LEFT, padx=2)
        Button(bar, text='保存配置', command=self._save_config, width=9).pack(side=LEFT, padx=2)

    def _show_progress(self, text='打包中...'):
        self.progress_bar.pack(side=LEFT, fill='x', expand=True, padx=(0, 8))
        self.progress_status.config(text=text)
        self.progress_status.pack(side=LEFT)
        self.progress_bar.start(10)
        self.btn_build.config(state=DISABLED)
        self.btn_cancel.config(state=NORMAL, text='■  取消打包')
        self.btn_cancel.pack(side=LEFT, padx=4)

    def _hide_progress(self):
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        self.progress_status.pack_forget()
        self.btn_build.config(state=NORMAL)
        self.btn_cancel.pack_forget()
        self.btn_cancel.config(state=NORMAL, text='■  取消打包')

    def _cancel_build(self):
        self._cancel_flag[0] = True
        self._log('[取消] 正在停止打包...')
        self.btn_cancel.config(state=DISABLED, text='取消中...')

    # ══════════════ 配置 ══════════════
    def _gather_config(self):
        return {
            'config_version': CONFIG_VERSION,
            'entry_script': self.entry_script.get(),
            'project_dir': self.project_dir.get(),
            'python_exe': self.python_exe.get(),
            'mode': self.mode_var.get(),
            'onefile': self.onefile_var.get(),
            'no_console': self.no_console_var.get(),
            'disable_traceback': self.disable_traceback_var.get(),
            'uac_admin': self.uac_admin_var.get(),
            'icon': self.icon_var.get(),
            'version_file': self.version_file_var.get(),
            'splash': self.splash_var.get(),
            'name': self.name_var.get(),
            'output_dir': self.output_dir_var.get(),
            'clean': self.clean_var.get(),
            'debug': self.debug_var.get(),
            'upx_dir': self.upx_dir_var.get(),
            'workpath': self.workpath_var.get(),
            'runtime_tmpdir': self.runtime_tmpdir_var.get(),
            'exclude': self.exclude_var.get(),
            'policy': self.policy_var.get(),
            'hook': self.hook_var.get(),
            'test_run': self.test_run_var.get(),
            'keep_temp': self.keep_temp_var.get(),
            'light_version': self.light_version.get(),
            'data_files': list(self.data_files),
            'selected_modules': sorted(self.detect.get('selected', set())),
        }

    def _apply_config(self, cfg):
        old = cfg.get('config_version') != CONFIG_VERSION
        bad = []

        def as_text(v):
            if v is None:
                return ''
            return v if isinstance(v, str) else str(v)

        def as_bool(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                low = v.strip().lower()
                if low in ('1', 'true', 'yes', 'on', '是'):
                    return True
                if low in ('0', 'false', 'no', 'off', '否', ''):
                    return False
            raise ValueError(f'不是布尔值 {v!r}')

        def put(var, key, default, cast):
            """一个字段坏掉不能连累其它字段：以前整段一个 try，
            第 20 个字段抛异常，后面所有设置全丢，关窗时还会把默认值覆盖写回。"""
            try:
                val = cfg.get(key, default)
                if val is None:
                    val = default
                var.set(cast(val))
            except Exception as exc:
                bad.append(f'{key} ({exc})')

        put(self.entry_script, 'entry_script', '', as_text)
        put(self.project_dir, 'project_dir', '', as_text)
        try:
            self.python_exe.set(cfg.get('python_exe') or sys.executable)
        except Exception as exc:
            bad.append(f'python_exe ({exc})')

        mode = cfg.get('mode', 0)
        try:
            mode = int(mode or 0)
        except (TypeError, ValueError):
            mode = 0
        if mode not in (0, 1, 2, 3, 4):
            mode = 0
        if old:
            # 旧版 → 现版模式编号变了: 旧的 1(捆绑数据/onefile) 并入新 0, 旧的 2 保持 2
            mode = {0: 0, 1: 0, 2: 2, 3: 3}.get(mode, 0)
        try:
            self.mode_var.set(mode)
        except Exception as exc:
            bad.append(f'mode ({exc})')

        put(self.onefile_var, 'onefile', True, as_bool)
        put(self.no_console_var, 'no_console', False, as_bool)
        put(self.disable_traceback_var, 'disable_traceback', False, as_bool)
        put(self.uac_admin_var, 'uac_admin', False, as_bool)
        # uac-uiaccess 永远从配置里恢复为关闭: 老配置正是因为它打出了起不来的 exe
        try:
            self.uac_uiaccess_var.set(False)
        except Exception:
            pass
        put(self.icon_var, 'icon', '', as_text)
        put(self.version_file_var, 'version_file', '', as_text)
        put(self.splash_var, 'splash', '', as_text)
        put(self.name_var, 'name', '', as_text)
        put(self.output_dir_var, 'output_dir', '', as_text)
        put(self.clean_var, 'clean', True, as_bool)
        put(self.debug_var, 'debug', '', as_text)
        put(self.upx_dir_var, 'upx_dir', '', as_text)
        put(self.workpath_var, 'workpath', '', as_text)
        put(self.runtime_tmpdir_var, 'runtime_tmpdir', '', as_text)
        put(self.exclude_var, 'exclude', '', as_text)

        def as_policy(v):
            return v if v in ('auto', 'manual', 'none') else 'auto'

        put(self.policy_var, 'policy', 'auto', as_policy)
        put(self.hook_var, 'hook', True, as_bool)
        put(self.test_run_var, 'test_run', True, as_bool)
        put(self.keep_temp_var, 'keep_temp', False, as_bool)
        put(self.light_version, 'light_version', '1.0.0.0', lambda v: as_text(v) or '1.0.0.0')

        try:
            self.data_files.clear()
            self.data_listbox.delete(0, END)
            raw_data = cfg.get('data_files')
            if not isinstance(raw_data, list):
                raw_data = []          # 配置被改坏成字符串时别逐字符拆开
            for f in raw_data:
                if f and isinstance(f, str):
                    self.data_files.append(f)
                    self.data_listbox.insert(END, f)
        except Exception as exc:
            bad.append(f'data_files ({exc})')

        # 手动勾选过的模块：不恢复的话重启后会被静默"全部打入"
        raw_sel = cfg.get('selected_modules')
        if isinstance(raw_sel, list):
            self._saved_selected = [s for s in raw_sel if isinstance(s, str)]
        else:
            self._saved_selected = []

        if bad:
            self._queue_log('[配置] 有 %d 项没恢复出来，其余已正常加载: %s'
                            % (len(bad), '; '.join(bad[:5])))
            # 原文件先备份一份，万一被我们写回时不完整
            try:
                shutil.copy2(CONFIG_PATH, CONFIG_PATH + '.bak')
                self._queue_log(f'[配置] 原配置已备份: {CONFIG_PATH}.bak')
            except Exception:
                pass

        if old and cfg.get('uac_uiaccess'):
            self._queue_log('[修复] 检测到旧配置里开着 --uac-uiaccess, '
                            '这正是 exe 报「从服务器返回了一个参照」的原因, '
                            '已自动关闭。重新打包即可。')

    def _save_config(self, silent=False):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._gather_config(), f, ensure_ascii=False, indent=2)
            if not silent:
                self._log('[配置] 已保存')
        except Exception as exc:
            if not silent:
                self._log(f'[配置] 保存失败: {exc}')

    def _load_config(self):
        if not os.path.isfile(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            self._apply_config(cfg)
            self._log('[配置] 已加载上次设置')
        except Exception as exc:
            self._log(f'[配置] 读取失败: {exc}')

    def _on_close(self):
        if self._building:
            messagebox.showinfo('正在打包', '打包还在进行，请先点「取消打包」再关闭窗口。')
            return
        self._save_config(silent=True)
        self.root.destroy()

    # ══════════════ 模式 ══════════════
    def _on_mode_change(self):
        mode = self.mode_var.get()
        prev = getattr(self, '_ui_mode', None)
        # 模式本身就决定了形态，切回来必须把它复位。
        # 之前只有 mode2 强制 onedir，从文件夹模式切回「单文件」时 onefile 还是 False，
        # 所谓单文件模式会打出一个文件夹，确认框里两行还互相打脸。
        if mode in (0, 1, 3):
            self.onefile_var.set(True)
        elif mode == 2:
            self.onefile_var.set(False)
        if mode == 1:
            self.hook_var.set(True)
            if not self.project_dir.get().strip() and self.entry_script.get().strip():
                self.project_dir.set(os.path.dirname(
                    os.path.abspath(self.entry_script.get().strip())))
        if prev is not None and prev != mode:
            self._sync_auto_name(prev, mode)
        self._refresh_mode_ui()

    def _auto_name(self, mode, entry):
        """按模式给出默认输出名（不含扩展名）：轻量模式带 -需PY 后缀"""
        suffix = LIGHT_SUFFIX if mode == 4 else ''
        if not entry:
            return 'app' + suffix
        path = entry
        if path.lower().endswith('.spec'):
            path = path[:-5] + '.py'
        return (Path(path).stem or 'app') + suffix

    def _sync_auto_name(self, prev_mode, mode):
        """切换模式时把自动填的名字换成对应形式；用户自己改过的不动"""
        entry = self.entry_script.get().strip()
        if not entry:
            return
        cur = self.name_var.get().strip()
        if not cur or cur == self._auto_name(prev_mode, entry):
            self.name_var.set(self._auto_name(mode, entry))

    def _adopt_name(self, new_entry, new_mode):
        """换了入口脚本/项目目录之后，自动名要跟着换。

        以前这两条路径只在名字为空时才填，于是选完 B 脚本，名字还挂在 A 上；
        模式5 里点「选目录…」切到目录模式，名字还留着 `-需PY` 后缀，
        打出来一个叫 xxx-需PY.exe 的完整自包含包，误导接收方。
        用户手填过的名字绝不动。
        """
        cur = self.name_var.get().strip()
        old_entry = self.entry_script.get().strip()
        old_mode = getattr(self, '_ui_mode', new_mode)
        auto = self._auto_name(old_mode, old_entry) if old_entry else ''
        if not cur or (auto and cur == auto):
            self.name_var.set(self._auto_name(new_mode, new_entry))

    def _unique_name(self, distpath, name, onefile):
        """输出目录里已有同名文件时自动加 (1)(2)…，不覆盖别人的产物"""
        if not os.path.isdir(distpath):
            return name

        def taken(n):
            if os.path.exists(os.path.join(distpath, n + '.exe')):
                return True
            return (not onefile) and os.path.isdir(os.path.join(distpath, n))

        if not taken(name):
            return name
        i = 1
        while taken('%s (%d)' % (name, i)):
            i += 1
        return '%s (%d)' % (name, i)


    def _on_onefile_toggle(self):
        mode = self.mode_var.get()
        if mode == 1 and not self.onefile_var.get():
            self.onefile_var.set(True)
            self._log('[提示] 模式2 固定单文件, 已自动勾回')
        elif mode == 2 and self.onefile_var.get():
            self.onefile_var.set(False)
            self._log('[提示] 模式3 固定 onedir, 已自动取消单文件')
        self._refresh_mode_ui()

    def _refresh_mode_ui(self):
        mode = self.mode_var.get()
        light = (mode == 4)
        tip = {0: '单文件', 1: '目录模式(整个项目 → 单 exe)',
               2: '文件夹模式(onedir)', 3: '.spec 施工图',
               4: '轻量启动器(不打包 Python)'}.get(mode, '')
        self._ui_mode = mode      # 记下来，切模式时才知道要不要换自动名

        # 形态由模式决定，不让人再手动改一遍再自相矛盾。
        # （配置里存着 onefile=false 也是从这纠正回来，否则模式1 会打出文件夹）
        if mode == 2:
            self.onefile_var.set(False)
        elif mode in (0, 1, 3):
            self.onefile_var.set(True)

        # 这些模式用不到项目目录，置灰免得让人以为要选
        # 但「选目录…」是切到目录模式的入口，不能一起灰掉
        no_project = mode in (0, 4)
        state = DISABLED if light else NORMAL
        gray = '#9e9e9e'
        for widget in (self.project_entry, self.project_btn):
            try:
                widget.config(state=DISABLED if no_project else NORMAL,
                              disabledforeground=gray)
            except Exception:
                pass
        for key in ('clean', 'hook', 'notb'):
            widget = self.chk.get(key)
            if widget is not None:
                try:
                    widget.config(state=state, disabledforeground=gray)
                except Exception:
                    pass
        # 「单文件」勾选框只用来显示当前形态，改由②的模式决定
        w = self.chk.get('onefile')
        if w is not None:
            try:
                w.config(state=DISABLED, disabledforeground='#555',
                         text='单文件 (--onefile，由②的模式决定)')
            except Exception:
                pass
        # .spec 模式下，单文件/勾子/图标/数据文件这些全由 spec 文件决定，
        # PyInstaller 传了还会直接报 option(s) not allowed。置灰免得以为生效了。
        if mode == 3:
            for key in ('onefile', 'hook'):
                widget = self.chk.get(key)
                if widget is not None:
                    try:
                        widget.config(state=DISABLED, disabledforeground=gray)
                    except Exception:
                        pass
        # 参数面板整体切换：轻量模式只显示它自己那两项
        try:
            if light:
                self.pane_pyi.pack_forget()
                self.pane_light.pack(fill='x')
            else:
                self.pane_light.pack_forget()
                self.pane_pyi.pack(fill='x')
        except Exception:
            pass
        for radio in self.policy_radios.values():
            try:
                radio.config(state=state, disabledforeground=gray)
            except Exception:
                pass
        try:
            self.picker_btn.config(state=state, disabledforeground=gray)
        except Exception:
            pass

        if light:
            self.project_hint.config(
                text='轻量模式不打包目录: 只把入口脚本(以及「数据文件」里指定的东西)内嵌进 exe。'
                     '要把整个项目带走请用模式2', fg='#c0392b')
            try:
                self.light_note.pack(fill='x', pady=(2, 0))
            except Exception:
                pass
        elif mode == 0:
            self.project_hint.config(
                text='单文件模式只打入口脚本这一个文件（外加「数据文件」里指定的东西），用不到项目目录。'
                     '要把整个目录带走，用模式2 或模式3', fg='gray')
            try:
                self.light_note.pack_forget()
            except Exception:
                pass
        else:
            self.project_hint.config(
                text='目录模式下: 这个目录里的所有资源/模块会被打进 exe; 留空则用入口脚本所在的目录',
                fg='gray')
            try:
                self.light_note.pack_forget()
            except Exception:
                pass

        if not hasattr(self, 'home_status') or self._building:
            return
        if light:
            if self.light_ok:
                self.home_status.config(
                    text=f'当前模式: {tip} · {self.light_status}', fg='#1e8449')
            else:
                self.home_status.config(
                    text=f'模式5 暂不可用 —— {self.light_status}', fg='#c0392b')
        else:
            self.home_status.config(text=f'当前模式: {tip}', fg='gray')

    def _on_policy_change(self):
        policy = self.policy_var.get()
        if policy == 'none':
            self.policy_warn.pack(fill='x', pady=(4, 0))
        else:
            self.policy_warn.pack_forget()
        if policy == 'manual' and self.detect.get('done'):
            self.root.after(50, self._open_module_picker)
        self._refresh_module_summary()

    # ══════════════ 浏览 ══════════════
    def _browse_entry_file(self):
        path = filedialog.askopenfilename(
            title='选择入口脚本',
            filetypes=[('Python', '*.py *.pyw'), ('SPEC', '*.spec'), ('所有', '*.*')])
        if not path:
            return
        self._adopt_name(path, self.mode_var.get())
        self.entry_script.set(path)
        folder = os.path.dirname(os.path.abspath(path))
        if not self.project_dir.get().strip():
            self.project_dir.set(folder)
        if not self.name_var.get().strip():
            self.name_var.set(self._auto_name(self.mode_var.get(), path))
        self._log(f'[入口] {path}')
        self._start_detect(True)

    def _browse_project_dir(self):
        folder = filedialog.askdirectory(title='选择整个项目目录')
        if folder:
            self._use_project_dir(folder)

    def _browse_project_dir_only(self):
        folder = filedialog.askdirectory(title='选择项目目录')
        if folder:
            self.project_dir.set(folder)
            self._start_detect(True)

    def _use_project_dir(self, folder):
        self.project_dir.set(folder)
        files = collect_py_files(folder)
        if not files:
            messagebox.showwarning('没有脚本', f'{folder}\n这个目录里没有 .py 文件。')
            return
        if len(files) == 1:
            pick = files[0]
        else:
            pick = EntryPicker(self.root, folder, files).show()
        if not pick:
            return
        self._adopt_name(pick, 1)
        self.entry_script.set(pick)
        self.mode_var.set(1)
        if not self.name_var.get().strip():
            self.name_var.set(self._auto_name(1, pick))
        self.onefile_var.set(True)
        self.hook_var.set(True)
        self._refresh_mode_ui()
        self._log(f'[项目] {folder}')
        self._log(f'[入口] {pick}')
        self._start_detect(True)

    def _clear_entry(self):
        self.entry_script.set('')
        self.detect = self._empty_detect()
        self._refresh_module_summary()
        self._fill_mod_tree()

    def _browse_icon(self):
        path = filedialog.askopenfilename(title='选择图标 (.ico)',
                                          filetypes=[('ICO', '*.ico'), ('所有', '*.*')])
        if path:
            if not path.lower().endswith('.ico'):
                messagebox.showwarning('格式', 'PyInstaller 只认 .ico')
                return
            self.icon_var.set(path)

    def _browse_output_dir(self):
        path = filedialog.askdirectory(title='选择输出目录')
        if path:
            self.output_dir_var.set(path)
            self._log(f'[输出] {path}')

    def _browse_version(self):
        path = filedialog.askopenfilename(title='版本信息文件',
                                          filetypes=[('文本', '*.txt *.py'), ('所有', '*.*')])
        if path:
            self.version_file_var.set(path)

    def _browse_splash(self):
        path = filedialog.askopenfilename(title='启动画面 (.png)',
                                          filetypes=[('PNG', '*.png'), ('所有', '*.*')])
        if path:
            self.splash_var.set(path)

    def _browse_upx_dir(self):
        path = filedialog.askdirectory(title='UPX 目录')
        if path:
            self.upx_dir_var.set(path)

    def _browse_workpath(self):
        path = filedialog.askdirectory(title='临时构建目录')
        if path:
            self.workpath_var.set(path)

    def _browse_runtime_tmpdir(self):
        path = filedialog.askdirectory(title='运行时解压目录')
        if path:
            self.runtime_tmpdir_var.set(path)

    def _generate_version_template(self):
        try:
            path = filedialog.asksaveasfilename(title='保存版本模板',
                                               defaultextension='.txt',
                                               filetypes=[('文本', '*.txt')])
            if not path:
                return
            content = VERSION_FILE_TEMPLATE.format(
                major=1, minor=0, patch=0, version='1.0.0', company='我的公司',
                desc='程序描述', internal='内部名', copyright='Copyright 2026',
                orig='app.exe', product='我的程序')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.version_file_var.set(path)
            self._log(f'[版本模板] {path}')
        except Exception as exc:
            messagebox.showerror('失败', str(exc))

    # ══════════════ 数据文件 ══════════════
    def _add_data_file(self):
        paths = filedialog.askopenfilenames(title='选择数据文件')
        for p in paths:
            if p not in self.data_files:
                self.data_files.append(p)
                self.data_listbox.insert(END, p)
        if paths:
            self._log(f'[数据] +{len(paths)} 个文件')

    def _add_data_dir(self):
        path = filedialog.askdirectory(title='选择目录')
        if path and path not in self.data_files:
            self.data_files.append(path)
            self.data_listbox.insert(END, path + os.sep)

    def _remove_data_file(self):
        for idx in reversed(list(self.data_listbox.curselection())):
            self.data_files.pop(idx)
            self.data_listbox.delete(idx)

    def _clear_data_files(self):
        self.data_files.clear()
        self.data_listbox.delete(0, END)

    # ══════════════ 解释器 ══════════════
    def _scan_interp(self):
        if self._scan_running:
            return
        self._scan_running = True
        self._log('[扫描] 正在找本机的 Python...')

        def work():
            try:
                items = scan_interpreters(self._queue_log)
            except Exception:
                items = []
            self._interp_list = items
            self._queue_log(f'[扫描] 共找到 {len(items)} 个可用的解释器')
            # 不在工作线程里碰 Tk, 交给主线程心跳去弹列表
            self._interp_ready = True

        threading.Thread(target=work, daemon=True).start()

    def _show_interp_list(self):
        self._scan_running = False
        items = self._interp_list
        if not items:
            messagebox.showwarning('没找到', '没扫描到可用的 Python，请手动「浏览…」选一个 python.exe')
            return
        self.interp_combo['values'] = [i['exe'] for i in items]
        pick = InterpreterPicker(self.root, items, self.python_exe.get()).show()
        if pick:
            self._set_interp(pick)

    def _browse_interp(self):
        path = filedialog.askopenfilename(
            title='选择 python.exe',
            filetypes=[('Python', 'python*.exe'), ('程序', '*.exe'), ('所有', '*.*')])
        if not path:
            return
        info = python_info(path)
        if not info:
            messagebox.showerror('不可用', f'{path}\n不是可用的 Python 解释器(启动失败)')
            return
        self._set_interp(info)

    def _set_interp(self, info):
        self.python_exe.set(info['exe'])
        self._log(f"[解释器] Python {info['version']} ({info['bits']}位)  {info['exe']}")
        if info['embedded']:
            self._log('[警告] 这是嵌入式 Python: 默认没有 pip / site-packages, '
                      '请确认它里面已经有你要打包的库, 否则打出来的 exe 会启动即崩')
        if not info['pyinstaller']:
            self._log('[警告] 这个解释器里没有 PyInstaller, 点「装 PyInstaller」给它装一个')
            self.home_status.config(text='该解释器缺少 PyInstaller', fg='#c0392b')
        else:
            self.home_status.config(text=f"PyInstaller {info['pyinstaller']} · Python {info['version']}",
                                    fg='#1e8449')
        if self.entry_script.get().strip():
            self._start_detect(False)

    def _install_pyinstaller(self):
        exe = self.python_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror('解释器无效', '先选择一个有效的 python.exe')
            return
        if not messagebox.askyesno(
                '安装 PyInstaller',
                f'给这个解释器安装 PyInstaller?\n\n{exe}\n\n'
                f'会执行: python -m pip install -U pyinstaller\n需要联网。'):
            return

        self._log(f'[安装] {exe} -m pip install -U pyinstaller')

        def work():
            rc, out = run_capture([exe, '-m', 'pip', 'install', '-U', 'pyinstaller'],
                                  timeout=600)
            for line in out.splitlines():
                if line.strip():
                    self._queue_log('[pip] ' + line.strip())
            if rc == 0:
                info = python_info(exe)
                if info and info['pyinstaller']:
                    self._queue_log(f"[安装] 成功, PyInstaller {info['pyinstaller']}")
                else:
                    self._queue_log('[安装] pip 返回成功但仍旧找不到 PyInstaller')
            else:
                self._queue_log(f'[安装] 失败(返回码 {rc})。嵌入式 Python 一般没有 pip, '
                                f'建议换成标准安装的 Python。')

        threading.Thread(target=work, daemon=True).start()

    # ══════════════ 检测 ══════════════
    def _start_detect(self, show_dialog):
        entry = self.entry_script.get().strip()
        target = self.project_dir.get().strip() or (os.path.dirname(entry) if entry else '')
        if not entry and not target:
            return
        if self._detect_running:
            # 上一轮还没跑完又来了新请求(用户手快连选两个脚本)。
            # 直接丢弃会让打包用上一个脚本的模块清单，所以记下来等会儿补跑。
            self._detect_pending = show_dialog
            self.module_status.config(text='正在检测模块…(最新一次已排队)', fg='#555')
            return

        self._detect_running = True
        self._detect_pending = None
        scope = self._expected_scope(self.mode_var.get(), entry, target)
        self.module_status.config(text='正在检测模块…', fg='#555')
        self.root.config(cursor='watch')
        self._log(f'[检测] 扫描范围: {scope}')

        py = self.python_exe.get().strip() or sys.executable

        def work():
            try:
                payload = self._detect_worker(scope, py, show_dialog)
            except Exception:
                self._queue_log('[检测] 异常: ' + traceback.format_exc(limit=3))
                payload = None
            self._detect_result = payload if payload is not None else 'EMPTY'

        threading.Thread(target=work, daemon=True).start()

    def _resume_pending_detect(self):
        """上一轮结束后补跑排队的那次检测。返回 True 表示有排队任务。"""
        pending = getattr(self, '_detect_pending', None)
        self._detect_pending = None
        if pending is None:
            return False
        self.root.after(30, lambda: self._start_detect(pending))
        return True

    def _detect_worker(self, scope, py, show_dialog):
        if os.path.isdir(scope):
            files = collect_py_files(scope)
        elif os.path.isfile(scope):
            files = [scope]
            # 入口脚本同目录的兄弟模块也算进来(项目通常是散的)
            pass
        else:
            files = []

        if not files:
            self._queue_log('[检测] 没找到 .py 文件')
            return None

        names, subs, local, errors = analyze_imports(files)
        self._queue_log(f'[检测] {len(files)} 个 .py 文件, 发现 {len(names)} 个导入名')
        if scan_truncated():
            self._queue_log('[检测] ⚠ 项目里的 .py 太多，只扫了一部分 —— '
                            '模块清单可能不全，建议把项目目录缩小到真正要打包的那一层')
        for err in errors[:5]:
            self._queue_log(f'[检测] 跳过无法解析的文件: {err}')

        cls = classify_modules(py, names, subs, local)
        if cls.get('probe_failed'):
            # 解释器跑不起来时不能报"全部缺失"，那会让人去 pip install os
            self._queue_log(f'[检测] ✗ 用 {py} 探测模块失败（解释器起不来或超时）。'
                            '这次不判断哪些模块缺失，请先在③区换一个能用的解释器。')
            cls['missing'] = {}
        self._queue_log(
            f"[检测] 第三方 {len(cls['third'])} / 标准库 {len(cls['stdlib'])} / "
            f"本地 {len(cls['local'])} / 缺失 {len(cls['missing'])}")

        selected = set(cls['third'].keys())
        return (files, cls, selected, errors, scope, show_dialog)

    def _detect_done_ui(self):
        self._detect_running = False
        try:
            self.root.config(cursor='')
        except Exception:
            pass

    def _apply_detection(self, files, cls, selected, errors, scope, show_dialog):
        self._detect_done_ui()
        previous = self.detect
        keep = set(selected)
        # 手动模式下重新检测，要保住用户已经勾掉的那些，否则一按「重新检测」
        # 又变回「全部打入」，打包内容就和用户的选择不一致了。
        # 换了扫描范围(换了脚本/目录)则重置，那是另一批模块。
        if (self.policy_var.get() == 'manual' and previous.get('done')
                and previous.get('scope') == scope):
            old = set(previous.get('selected', set()))
            keep = {name for name in cls['third'] if name in old}
        else:
            # 上一次会话里手动勾过的清单：不恢复的话重启后会静默变回"全部打入"
            saved = getattr(self, '_saved_selected', None)
            if self.policy_var.get() == 'manual' and saved:
                keep = {name for name in cls['third'] if name in set(saved)}
                self._saved_selected = None

        self.detect = {
            'files': files, 'third': cls['third'], 'stdlib': cls['stdlib'],
            'local': cls['local'], 'missing': cls['missing'],
            'selected': keep, 'errors': errors, 'scope': scope, 'done': True,
        }
        self._refresh_module_summary()
        self._fill_mod_tree()

        # 有排队的新检测：这轮结果已经过期，不弹窗，直接接着跑
        if self._resume_pending_detect():
            return

        if not show_dialog:
            return

        missing = sorted(self.detect['missing'].keys())
        summary = (f'扫描范围: {scope}\n'
                   f'共 {len(files)} 个 .py 文件\n\n'
                   f'第三方模块: {len(self.detect["third"])} 个\n'
                   f'标准库: {len(self.detect["stdlib"])} 个\n'
                   f'项目自身模块: {len(self.detect["local"])} 个\n'
                   f'缺失(解释器里没有): {len(missing)} 个')
        policy = PolicyDialog(self.root, summary, missing).show()
        if policy:
            self.policy_var.set(policy)
            self._on_policy_change()

    def _refresh_module_summary(self):
        d = self.detect
        if not d.get('done'):
            self.module_status.config(text='尚未检测 —— 选一个入口脚本或目录就会自动开始',
                                      fg='#777')
            return
        third = len(d['third'])
        miss = len(d['missing'])
        loc = len(d['local'])
        policy = self.policy_var.get()
        sel = len(self._hidden_imports())
        label = {'auto': '自动', 'manual': '手动', 'none': '不打包'}.get(policy, policy)
        text = (f'已检测: 第三方 {third} · 缺失 {miss} · 项目内模块 {loc} · '
                f'将打入 {sel} 个隐藏导入 · 策略: {label}')
        color = '#1e8449' if miss == 0 else '#c0392b'
        if policy == 'none':
            color = '#c0392b'
        self.module_status.config(text=text, fg=color)

        if policy == 'none':
            self.policy_warn.pack(fill='x', pady=(4, 0))
        else:
            self.policy_warn.pack_forget()

    def _fill_mod_tree(self):
        if not hasattr(self, 'mod_tree'):
            return
        for item in self.mod_tree.get_children():
            self.mod_tree.delete(item)
        d = self.detect
        if not d.get('done'):
            return
        rows = []
        for name, info in d['third'].items():
            rows.append((name, '第三方', info.get('version', ''), '可用',
                         info.get('file', ''), ''))
        for name in d['missing']:
            rows.append((name, '未知', '', '缺失', '', 'missing'))
        for name in d['local']:
            rows.append((name, '项目内', '', '自带', '', 'local'))
        for name in d['stdlib']:
            rows.append((name, '标准库', '', '内置', '', 'std'))
        rows.sort(key=lambda r: (r[1] != '第三方', r[0].lower()))
        for name, kind, ver, status, path, tag in rows:
            self.mod_tree.insert('', END, values=(name, kind, ver, status, path),
                                 tags=(tag,) if tag else ())

    def _hidden_imports(self):
        d = self.detect
        if not d.get('done'):
            return []
        policy = self.policy_var.get()
        if policy == 'none':
            return []
        if policy == 'auto':
            chosen = set(d['third'].keys())
        else:
            chosen = set(d['selected']) & set(d['third'].keys())
        result = set(chosen)
        for name in list(chosen):
            for extra in EXTRA_HIDDEN.get(name, []):
                result.add(extra)
        return sorted(result)

    def _open_module_picker(self):
        d = self.detect
        if not d.get('done'):
            messagebox.showinfo('还没检测', '先选一个入口脚本/目录，等检测跑完再勾。')
            return

        rows = []
        for name, info in sorted(d['third'].items()):
            rows.append((name, '第三方', info.get('version', ''), '可用'))
        for name in sorted(d['missing']):
            rows.append((name, '未知', '', '缺失'))
        if not rows:
            messagebox.showinfo('没有可选的', '这个项目没有第三方依赖，全是标准库。')
            return

        if self.policy_var.get() == 'auto':
            pre = set(d['third'].keys())
        else:
            pre = set(d['selected'])

        picked = ModuleSelectDialog(self.root, rows, pre).show()
        if picked is None:
            return
        self.detect['selected'] = set(picked)
        self.policy_var.set('manual')
        self._refresh_module_summary()
        self._log(f'[模块] 手动选择 {len(picked)} 个: {", ".join(sorted(picked)) or "(空)"}')

    # ══════════════ 启动检查 ══════════════
    def _startup_check(self):
        # Tk 变量必须在主线程读; 工作线程只做 subprocess 探测
        exe = self.python_exe.get().strip() or sys.executable
        result = []
        self._startup_result = None

        def work():
            try:
                info = python_info(exe)
            except Exception:
                info = None
            # False 表示「查过了但不可用」, 和「还没查完(None)」区分开
            self._startup_result = info if info else False

        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, self._poll_startup)

    def _poll_startup(self):
        res = getattr(self, '_startup_result', None)
        if res is None:
            self.root.after(200, self._poll_startup)
            return
        self._startup_result = None
        self._apply_startup_check(res or None)

    def _apply_startup_check(self, info):
        if not info:
            self.home_status.config(text='当前解释器不可用, 请点「扫描本机」换一个', fg='#c0392b')
            self._log('[错误] 当前解释器不可用')
            return
        self._log(f"[就绪] Python {info['version']} ({info['bits']}位)  {info['exe']}")
        if info['pyinstaller']:
            self._log(f"[就绪] PyInstaller {info['pyinstaller']}")
            self.home_status.config(
                text=f"Python {info['version']} · PyInstaller {info['pyinstaller']} · 就绪",
                fg='#1e8449')
        else:
            self._log('[警告] 这个解释器没有 PyInstaller, 点「装 PyInstaller」或换一个解释器')
            self.home_status.config(text='缺少 PyInstaller —— 点「装 PyInstaller」',
                                    fg='#c0392b')
        if info['embedded']:
            self._log('[提示] 这是嵌入式 Python, 注意它里面有没有你要用的第三方库')

    # ══════════════ 打包 ══════════════
    def _resolve_paths(self):
        mode = self.mode_var.get()
        entry = self.entry_script.get().strip()
        # .spec 自动改判成模式4，但轻量模式(5)必须拦住：
        # 轻量模式拿到 .spec 会静默走 PyInstaller 分支，打出一个几十 MB 的包，
        # 而用户以为自己得到的是十几 KB 的引导器。
        self._spec_in_light = (mode == 4 and entry.lower().endswith('.spec'))
        if entry.lower().endswith('.spec') and mode != 4:
            mode = 3
        project = self.project_dir.get().strip()
        if mode in (0, 4) or not project:
            # 单文件 / 轻量模式不认项目目录（界面上也是灰的），别拿残留值影响输出位置
            project = os.path.dirname(os.path.abspath(entry)) if entry else ''

        # 名字留空时按模式自动生成（轻量模式带 -需PY，一眼能看出要目标机装 Python）
        default_name = self._auto_name(mode, entry)
        name = self.name_var.get().strip() or default_name
        name = re.sub(r'[\\/:*?"<>|]', '_', name)

        out = self.output_dir_var.get().strip()
        if out:
            distpath = os.path.abspath(out)
        else:
            base = project or (os.path.dirname(os.path.abspath(entry)) if entry else SCRIPT_DIR)
            distpath = os.path.join(base, 'dist')
        # .spec 的名字由 spec 自己决定，不参与重名改名
        if mode != 3:
            name = self._unique_name(distpath, name, self.onefile_var.get())
        return mode, entry, project, name, distpath

    def _expected_scope(self, mode, entry, project):
        """这次应该扫哪个范围：目录模式扫整个项目，其它模式扫入口脚本。
        检测时和打包前校验时必须用同一套算法，否则会互相判定为"不一致"。"""
        project = project or (os.path.dirname(entry) if entry else '')
        if mode == 1 and project:
            return project
        return entry or project

    def _start_build(self):
        # 上一次还在跑就不要再起一个线程（两个线程抢同一份状态会乱）
        if getattr(self, '_building', False):
            self._log('[提示] 还在打包中，等这次结束再点')
            return
        # 检测还没跑完就打包，会拿到空/上一份模块清单，打出来的 exe 缺依赖
        if getattr(self, '_detect_running', False):
            messagebox.showinfo(
                '模块还没检测完',
                '⑥区的模块检测还在跑（通常一两秒）。\n\n'
                '等它显示检测结果后再点打包 —— 现在打包会漏掉第三方库，'
                '生成的 exe 换台电脑就会报 ModuleNotFoundError。')
            return

        mode, entry, project, name, distpath = self._resolve_paths()

        if getattr(self, '_spec_in_light', False):
            messagebox.showerror(
                '轻量模式不支持 .spec',
                '轻量启动器只能包一个 .py 脚本。\n\n'
                '要按 .spec 打包，把「② 打包模式」改成模式4。')
            return

        if not entry:
            messagebox.showerror('缺入口', '先在「主页 ①」选择入口脚本，或直接「选目录…」')
            return
        if not os.path.isfile(entry):
            messagebox.showerror('文件不存在', entry)
            return

        if mode == 1 and not os.path.isdir(project):
            messagebox.showerror('缺项目目录',
                                 '目录模式需要「项目目录」。\n点「选目录…」选整个项目文件夹。')
            return

        # 模块清单必须对得上当前入口。手动把路径敲/粘贴进输入框、或者
        # 从单文件切到目录模式，都会让清单停留在上一个范围上 —— 打出来的
        # exe 要么缺依赖，要么白塞一堆无关的库。
        # 模式5 也要查（它同样靠这份清单决定要提示哪些库）。
        if mode != 3:
            want = self._expected_scope(mode, entry, project)
            got = self.detect.get('scope') or ''
            if (not self.detect.get('done')
                    or os.path.normcase(got) != os.path.normcase(want)):
                self._log(f'[检测] 模块清单的范围是「{got or "未检测"}」，'
                          f'和当前入口「{want}」不一致，正在重新检测…')
                self._start_detect(False)
                messagebox.showinfo(
                    '先重新检测一下模块',
                    '模块清单和当前入口对不上，已自动开始重新检测。\n\n'
                    '等一两秒，⑥区出结果后再点打包。')
                return

        if mode == 4:
            # 轻量模式走另一条完全不同的路：不碰 PyInstaller
            self._start_light_build(entry, name, distpath)
            return

        py = self.python_exe.get().strip()
        if not py or not os.path.isfile(py):
            messagebox.showerror('解释器无效', '请选择一个有效的 python.exe（主页 ③）')
            return

        if mode != 3:
            info = python_info(py)
            if not info:
                messagebox.showerror('解释器不可用', f'{py}\n启动失败')
                return
            if not info['pyinstaller']:
                if messagebox.askyesno('缺 PyInstaller',
                                       f'这个解释器没有 PyInstaller:\n{py}\n\n现在给它装一个吗？'):
                    self._install_pyinstaller()
                return

        if mode == 3 and entry.lower().endswith('.spec'):
            params_set = []
            if self.no_console_var.get():
                params_set.append('--noconsole')
            if self.uac_admin_var.get():
                params_set.append('--uac-admin')
            if params_set:
                if not messagebox.askyesno(
                        '.spec 模式提示',
                        f'用 .spec 打包时，界面上的这些参数不生效（它们写在 .spec 里）:\n\n  '
                        f'{", ".join(params_set)}\n\n继续？'):
                    return

        # ── 模块缺失警告 ──
        missing = sorted(self.detect.get('missing', {}).keys())
        if missing and self.policy_var.get() != 'none':
            messagebox.showwarning(
                '有模块装不上',
                '这些模块在所选解释器里找不到，打进去也没用，exe 运行时还是会报错：\n\n  '
                + ', '.join(missing[:12])
                + ('\n  …' if len(missing) > 12 else '')
                + '\n\n要么先给这个解释器 pip install 这些库，要么换一个装了它们的解释器。')

        if self.policy_var.get() == 'none':
            if not messagebox.askyesno(
                    '确定不打模块?',
                    '你选了「不打包模块」。\n\n'
                    '不会强制打入任何第三方模块，一切交给 PyInstaller 自动分析。\n'
                    '在本机能跑，但换一台没装这些库的电脑，极大概率打不开\n'
                    '（双击闪退 / ModuleNotFoundError）。\n\n真的这样打包吗？',
                    default='no'):
                return

        icon = self.icon_var.get().strip()
        if icon and not os.path.isfile(icon):
            self._log(f'[警告] 图标不存在, 已忽略: {icon}')
            icon = ''
        vf = self.version_file_var.get().strip()
        if vf and not os.path.isfile(vf):
            self._log(f'[警告] 版本文件不存在, 已忽略: {vf}')
            vf = ''
        splash = self.splash_var.get().strip()
        if splash and not os.path.isfile(splash):
            splash = ''

        hidden = self._hidden_imports()
        exclude = [x.strip() for x in self.exclude_var.get().replace('，', ',').split(',')
                   if x.strip()]

        if not messagebox.askyesno('确认打包', self._build_summary(
                mode, entry, project, name, distpath, hidden)):
            return

        self._save_config(silent=True)
        self._clear_log()
        self._cancel_flag[0] = False
        self._show_progress('正在打包...')

        self._log('=' * 62)
        self._log(f'  打包 | 模式{mode} | {datetime.now():%Y-%m-%d %H:%M:%S}')
        self._log('=' * 62)

        build_started = time.time()

        try:
            os.makedirs(distpath, exist_ok=True)
        except OSError as exc:
            self._log(f'[错误] 输出目录建不了 {distpath}: {exc}')
            self._hide_progress()
            messagebox.showerror('输出目录不可用', f'{distpath}\n\n{exc}')
            return

        def _mk(path):
            """建构建目录；失败要还原界面，否则按钮会永久卡在禁用状态"""
            try:
                os.makedirs(path, exist_ok=True)
                return True
            except OSError as exc:
                self._log(f'[错误] 构建临时目录建不了 {path}: {exc}')
                self._hide_progress()
                messagebox.showerror(
                    '构建目录不可用',
                    f'{path}\n\n{exc}\n\n'
                    '如果「高级」页的「临时构建目录」填的是一个文件、或者在不存在的盘上，'
                    '把它清空（用默认值）再试。')
                return False

        if mode == 3:
            # .spec 是施工图, PyInstaller 不允许再和 --name/--onefile/--add-data 这些
            # makespec 选项一起传(会直接报 option(s) not allowed)。只给路径类参数。
            tmp = self.workpath_var.get().strip() or os.path.join(
                work_root(), f'spec_{int(time.time())}')
            if not _mk(tmp):
                return
            cmd = [py, '-m', 'PyInstaller', '--noconfirm',
                   '--distpath', distpath, '--workpath', tmp]
            if self.clean_var.get():
                cmd.append('--clean')
            cmd.append(entry)
            notes = []
            name = Path(entry).stem
            workdir = os.path.dirname(os.path.abspath(entry)) or None
        else:
            tmp = self.workpath_var.get().strip() or os.path.join(
                work_root(), f"{name}_{int(time.time())}")
            if not _mk(tmp):
                return
            self._build_workpath = tmp

            datas = []
            if mode == 1 and os.path.isdir(project):
                # 输出目录和构建目录都要排除: 它们可能在项目内部深处,
                # 不排除就会把上一次的 exe 打进来, 体积一次次翻倍
                pairs = project_data_pairs(project,
                                           exclude_paths=[distpath, tmp,
                                                          self.workpath_var.get().strip()])
                datas.extend(pairs)
                total = 0
                for src, _ in pairs:
                    try:
                        if os.path.isfile(src):
                            total += os.path.getsize(src)
                        else:
                            for r, _d, fs in os.walk(src):
                                for f in fs:
                                    try:
                                        total += os.path.getsize(os.path.join(r, f))
                                    except OSError:
                                        pass
                    except OSError:
                        pass
                self._log(f'[项目] 打包整个目录: {project} '
                          f'({len(pairs)} 个顶层条目, 约 {total / 1048576:.1f} MB)')
                junk = find_nested_junk(project)
                if junk:
                    self._log(f'[提醒] 这些子目录也会被一并打进去（体积/隐私）: '
                              + ', '.join(os.path.basename(p) for p in junk))
            for f in self.data_files:
                if os.path.exists(f):
                    # 目录要用自己的名字当目标，否则里面的子结构会被拍平
                    if os.path.isdir(f):
                        datas.append((f, os.path.basename(os.path.normpath(f))))
                    else:
                        datas.append((f, '.'))

            paths = [os.path.dirname(os.path.abspath(entry))]
            if mode == 1 and os.path.isdir(project):
                paths.insert(0, os.path.abspath(project))
                try:
                    for sub in sorted(os.listdir(project)):
                        full = os.path.join(project, sub)
                        if os.path.isdir(full) and sub.lower() not in JUNK_DIRS \
                                and any(f.endswith('.py') for f in os.listdir(full)
                                        if os.path.isfile(os.path.join(full, f))):
                            paths.append(full)
                except OSError:
                    pass
            paths = list(dict.fromkeys(paths))

            hooks = []
            if self.hook_var.get():
                hook_path = os.path.join(tmp, 'rth_chdir.py')
                with open(hook_path, 'w', encoding='utf-8') as f:
                    f.write(HOOK_CHDIR)
                hooks.append(hook_path)

            cfg = {
                'name': name, 'distpath': distpath, 'workpath': tmp,
                'onefile': self.onefile_var.get(), 'noconsole': self.no_console_var.get(),
                'clean': self.clean_var.get(), 'debug': self.debug_var.get(),
                'uac_admin': self.uac_admin_var.get(),
                'icon': icon, 'version_file': vf, 'splash': splash,
                'disable_windowed_traceback': self.disable_traceback_var.get(),
                'upx_dir': self.upx_dir_var.get().strip(),
                'runtime_tmpdir': self.runtime_tmpdir_var.get().strip(),
                'hooks': hooks, 'paths': paths,
                'hidden': hidden, 'exclude': exclude, 'datas': datas,
            }
            cmd, notes = build_pyinstaller_cmd(py, entry, cfg)
            workdir = os.path.dirname(os.path.abspath(entry)) or None

        for note in notes:
            self._log(f'[说明] {note}')
        if mode != 3:
            self._log(f'[隐藏导入] {len(hidden)} 个')
            if hidden:
                self._log('           ' + ', '.join(hidden[:25])
                          + (' …' if len(hidden) > 25 else ''))

        self._building = True
        self._build_result = None
        self._build_meta = {
            'entry': entry, 'name': name, 'distpath': distpath,
            'onefile': self.onefile_var.get(),
            # 目录模式/构建完成后要清理的临时目录
            'workpath': tmp if not self.keep_temp_var.get() else '',
            # 这个目录是我们自己生成的, 还是用户在「高级」页指定的
            'workpath_owned': not bool(self.workpath_var.get().strip()),
            'started': build_started,
            'uac_admin': self.uac_admin_var.get(),
            'test_run': self.test_run_var.get(),
            'mode': mode,
        }
        threading.Thread(target=self._build_worker, args=(cmd, workdir),
                         daemon=True).start()

    def _local_module_files(self, entry):
        """入口脚本 import 的、就在同目录的模块（单文件或包）。

        模式5 只嵌入入口脚本本身，这些文件必须显式带进 exe 才能被 import。
        """
        try:
            tops, _subs, _local, _err = analyze_imports([entry])
        except Exception:
            return []
        base = os.path.dirname(os.path.abspath(entry))
        found = []
        for name in sorted(tops):
            top = (name or '').split('.')[0]
            if not top:
                continue
            py = os.path.join(base, top + '.py')
            if os.path.isfile(py) and os.path.abspath(py) != os.path.abspath(entry):
                found.append(py)
                continue
            pkg = os.path.join(base, top)
            if os.path.isfile(os.path.join(pkg, '__init__.py')):
                found.append(pkg)
        return found

    # ── 模式5：轻量启动器 ──
    def _start_light_build(self, entry, name, distpath):
        if not self.light_ok or self.light_module is None:
            messagebox.showerror(
                '模式5 不可用',
                f'{self.light_status}\n\n'
                '轻量模式靠系统自带的 C# 编译器（.NET Framework 4.x 里的 csc.exe）\n'
                '把你的脚本编成一个十几 KB 的引导器。\n\n'
                'Win10 / Win11 默认都有；如果没有，把 light_stub.py 放在本程序旁边重试，\n'
                '或者改用模式1（PyInstaller，体积大但目标机什么都不用装）。')
            return

        icon = self.icon_var.get().strip()
        if icon and not os.path.isfile(icon):
            self._log(f'[警告] 图标不存在, 已忽略: {icon}')
            icon = ''
        if icon and not icon.lower().endswith('.ico'):
            messagebox.showerror('图标格式', '轻量模式只认 .ico')
            return

        console = not self.no_console_var.get()
        uac = self.uac_admin_var.get()
        # 界面上已经没有版本号这一项了，固定写 1.0.0.0（别用配置里的残留值）
        version = '1.0.0.0'
        data_files = list(self.data_files)
        modules = sorted(self.detect.get('third', {}) or {})
        build_py = self.python_exe.get().strip()

        # 轻量模式只嵌入入口脚本一个文件；它 import 的同目录模块默认不会被带上，
        # 打出来的 exe 会在别的机器上 ModuleNotFoundError。这里主动问一次。
        extras = [p for p in self._local_module_files(entry)
                  if os.path.abspath(p) not in {os.path.abspath(q) for q in data_files}]
        if extras:
            shown = '\n'.join(
                '    ' + os.path.basename(p) + ('\\' if os.path.isdir(p) else '')
                for p in extras[:10])
            if len(extras) > 10:
                shown += f'\n    …等 {len(extras)} 项'
            if messagebox.askyesno(
                    '还引用了本地模块',
                    f'入口脚本 import 了同目录的这些文件：\n\n{shown}\n\n'
                    '轻量模式默认只嵌入入口脚本一个文件，'
                    '打出来的 exe 换台电脑跑会报 ModuleNotFoundError。\n\n'
                    '要把它们一起嵌进 exe 吗？（只带 .py 源码，体积增加几 KB）'):
                data_files.extend(extras)
                for p in extras:
                    self._log(f'[轻量] 一并嵌入本地模块: {p}')
            else:
                extras = []          # 用户不要，日志里也别声称带了

        if not messagebox.askyesno('确认(轻量模式)',
                                   self._build_summary(4, entry, '', name,
                                                       distpath, modules)):
            return

        self._save_config(silent=True)
        self._clear_log()
        self._cancel_flag[0] = False
        self._show_progress('正在编译引导器...')

        self._log('=' * 62)
        self._log(f'  轻量打包（不打包 Python） | {datetime.now():%Y-%m-%d %H:%M:%S}')
        self._log('=' * 62)
        self._log(f'[轻量] 编译器状态: {self.light_status}')
        if extras:
            self._log(f'[轻量] 附带本地模块 {len(extras)} 项: '
                      + ', '.join(os.path.basename(p) for p in extras[:8])
                      + (' …' if len(extras) > 8 else ''))
        self._log(f'[轻量] 目标机需要: Python 3'
                  + (f' + {"、".join(modules)}' if modules else '（无第三方库）'))
        if uac:
            self._log('[说明] 已要求管理员权限，双击时会弹 UAC')

        self._building = True
        self._build_result = None
        self._build_meta = {
            'entry': entry, 'name': name, 'distpath': distpath,
            'onefile': True, 'workpath': '',
            'uac_admin': uac, 'test_run': self.test_run_var.get(), 'mode': 4,
        }
        threading.Thread(
            target=self._light_worker,
            args=(entry, distpath, name, icon, console, uac, version,
                  data_files, modules, build_py),
            daemon=True).start()

    def _light_worker(self, entry, distpath, name, icon, console, uac, version,
                      data_files, modules, build_py):
        exe, err = None, None
        try:
            exe, err = self.light_module.build(
                entry, distpath, name,
                icon=icon or None, console=console, uac_admin=uac,
                version=version, data_files=data_files, modules=modules,
                build_python=build_py, log=self._queue_log)
        except ValueError as exc:
            # 参数不合法：直接给一句话，别把 traceback 糊到用户脸上
            exe, err = None, str(exc)
        except Exception:
            err = traceback.format_exc(limit=4)
            self._queue_log('[轻量] 异常:\n' + err)
        self._build_result = (bool(exe), err or '', False)

    def _build_summary(self, mode, entry, project, name, distpath, hidden):
        names = {
            0: '单文件',
            1: '目录模式 (整个项目 → 单个 exe)',
            2: '文件夹模式 (onedir)',
            3: '.spec 施工图',
            4: '轻量启动器 (不打包 Python)',
        }
        if mode == 4:
            mods = sorted({m.split('.')[0] for m in (hidden or []) if m})
            lines = [
                '══════ 确认轻量打包 ══════',
                '',
                '模式: 轻量启动器 —— 不打包 Python',
                f'入口: {entry}',
                f'名称: {name}.exe   (约十几 KB)',
                f'输出: {distpath}',
                f'控制台: {"隐藏(无黑窗)" if self.no_console_var.get() else "显示"}',
                f'管理员权限: {"是 (启动弹UAC)" if self.uac_admin_var.get() else "否"}',
                f'图标: {self.icon_var.get().strip() or "(无)"}',
                f'内嵌数据: {len(self.data_files)} 个',
                '',
                '⚠ 目标电脑必须已经装好 Python 3'
                + (f'，并且有这些库: {"、".join(mods)}' if mods else '（你的脚本没用到第三方库）'),
                '   否则双击后会弹提示，告诉你缺什么、该 pip install 什么。',
                '',
                '开始打包?',
            ]
            return '\n'.join(lines)

        dbg = self.debug_var.get()
        dbg_lab = self.debug_labels.get(dbg, '关闭')
        policy = {'auto': '自动(全部打入)', 'manual': f'手动({len(hidden)} 个)',
                  'none': '不打包模块 ⚠'}.get(self.policy_var.get(), '?')
        lines = [
            '══════ 确认打包 ══════',
            '',
            f'模式: {names.get(mode, "?")}',
            f'入口: {entry}',
        ]
        if mode == 1:
            lines.append(f'项目目录: {project}')
            lines.append('         (整个目录会按原来的子目录结构打进 exe)')
        if mode == 3:
            lines += [
                f'名称: 由 .spec 里的 EXE(name=...) 决定',
                f'输出: {distpath}',
                '',
                '⚠ 用 .spec 打包时，这些界面选项不生效（都由 spec 文件决定）:',
                '   单文件/onedir、图标、数据文件、隐藏导入、运行时切资源目录',
                '',
                '开始打包?',
            ]
            return '\n'.join(lines)
        lines += [
            f'名称: {name}.exe' if mode != 2 else f'名称: {name}\\{name}.exe',
            f'输出: {distpath}',
            f'解释器: {self.python_exe.get().strip()}',
            f'单文件: {"是" if self.onefile_var.get() else "否 (onedir)"}',
            f'控制台: {"隐藏" if self.no_console_var.get() else "显示"}',
            f'管理员权限: {"是 (启动弹UAC)" if self.uac_admin_var.get() else "否"}',
            f'图标: {self.icon_var.get().strip() or "(无)"}',
            f'调试: {dbg_lab}',
            f'模块: {policy}',
            f'隐藏导入: {len(hidden)} 个',
            f'数据文件: {len(self.data_files)} 个',
            f'运行时切资源目录: {"开" if self.hook_var.get() else "关"}',
            '',
            '打包完成后会自动清理构建临时文件。',
            '',
            '开始打包?',
        ]
        return '\n'.join(lines)

    def _build_worker(self, cmd, workdir):
        success, err = False, None
        try:
            success, err = run_build(cmd, self._queue_log, workdir=workdir,
                                     cancel_flag=self._cancel_flag)
        except Exception:
            err = traceback.format_exc(limit=3)
            self._queue_log('[异常] ' + err)
        # 以构建本身的返回值为准。之前是事后去读 _cancel_flag, 会出现
        # 「PyInstaller 已经成功、用户恰好点了一下取消」→ 成功被当成取消丢掉
        cancelled = (not success) and (err == '用户取消')
        self._build_result = (success, err, cancelled)

    def _finish_build(self, success, err, cancelled):
        self._building = False
        self._drain_logs()
        meta = getattr(self, '_build_meta', {}) or {}
        self._log('')

        def _cleanup():
            cleanup_temp(meta.get('workpath'), self._queue_log,
                         meta.get('workpath_owned', True), meta.get('name', ''))

        if cancelled:
            self._log('  已 取 消')
            _cleanup()
            self._drain_logs()
            self._hide_progress()
            return

        if success:
            target = locate_product(meta.get('distpath', ''), meta.get('name', ''),
                                    meta.get('onefile', True),
                                    meta.get('started', 0))
            if not target:
                success = False
                err = f"打包返回成功但输出目录里找不到 exe: {meta.get('distpath')}"
            else:
                self.last_success_output = target
                self.last_dist_dir = (os.path.dirname(target) if os.path.isfile(target)
                                      else target)
                size = 0
                try:
                    if os.path.isfile(target):
                        size = os.path.getsize(target)
                    else:
                        for r, _d, fs in os.walk(target):
                            for f in fs:
                                try:
                                    size += os.path.getsize(os.path.join(r, f))
                                except OSError:
                                    pass
                except OSError:
                    pass
                self._log(f'[产物] {target}')
                if size >= 1048576:
                    self._log(f'[体积] {size / 1048576:.1f} MB')
                else:
                    self._log(f'[体积] {size / 1024:.1f} KB')

        self._log('=' * 62)
        if success:
            self._log('  打 包 成 功')
            self._log('=' * 62)
            if meta.get('workpath'):
                _cleanup()
            self._drain_logs()

            self._hide_progress()
            self._verify_output(meta)

            tgt = self.last_success_output
            open_dir = os.path.dirname(tgt) if os.path.isfile(tgt) else tgt
            try:
                os.startfile(open_dir)
            except Exception:
                pass
            messagebox.showinfo('打包完成', f'成功！\n\n产物:\n{tgt}')
        else:
            self._log(f'  失 败 | {err}')
            self._log('=' * 62)
            if meta.get('workpath'):
                self._log(f"[提示] 临时目录留着方便排查: {meta.get('workpath')}")
            self._hide_progress()
            messagebox.showwarning(
                '打包失败',
                f'{err}\n\n看一眼下面的构建日志，最后几十行通常就是真正的原因。\n'
                f'（常见: 模块找不到 / 路径有中文和空格 / 解释器没装 PyInstaller）')

    def _verify_output(self, meta):
        if not self.test_run_var.get() or not meta.get('test_run'):
            return
        target = self.last_success_output
        if not target or not os.path.isfile(target):
            self._queue_log('[自检] 找不到产物文件, 跳过试运行')
            return
        if not meta.get('onefile', True):
            self._queue_log('[自检] onedir 模式跳过自动试运行(请手动双击文件夹里的 exe)')
            return
        if meta.get('uac_admin'):
            self._queue_log('[自检] 跳过: 该 exe 要求管理员权限, 自动运行会弹 UAC')
            return

        # 放到后台跑: 之前在主线程 wait(5), 打包完成瞬间窗口会僵住 5 秒
        threading.Thread(target=self._verify_worker, args=(target,),
                         daemon=True).start()

    def _verify_worker(self, target):
        log = self._queue_log
        log('[自检] 试运行 5 秒, 看它是不是秒退...')
        try:
            proc = subprocess.Popen([target], cwd=os.path.dirname(target),
                                    creationflags=NO_WINDOW,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors='replace')
        except Exception as exc:
            log(f'[自检] 启动失败: {exc}')
            return
        try:
            out, err = proc.communicate(timeout=5)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            _kill(proc)
            log('[自检] ✓ 程序稳定运行超过 5 秒, 已自动关闭')
            return
        detail = ((err or '') + (out or '')).strip()
        if rc == 0:
            log('[自检] 程序运行后自行退出(返回码 0), 没有报错')
        else:
            log(f'[自检] ✗ 程序启动后很快退出, 返回码 {rc}')
            if detail:
                log('[自检] 它自己打印的最后几行:')
                for line in detail.splitlines()[-8:]:
                    log('    ' + line)
            log('[自检] 这通常意味着缺模块或资源路径不对。'
                '把「隐藏控制台黑窗」取消掉重打一次, 就能看到具体的报错。')

    def _test_last(self):
        if not self.last_success_output or not os.path.exists(self.last_success_output):
            messagebox.showinfo('还没有产物', '先打一次包。')
            return
        target = self.last_success_output
        if os.path.isfile(target):
            try:
                subprocess.Popen([target], cwd=os.path.dirname(target))
                self._log(f'[试运行] {target}')
            except Exception as exc:
                self._log(f'[试运行] 失败: {exc}')
        else:
            try:
                os.startfile(target)
            except Exception as exc:
                self._log(f'[试运行] 失败: {exc}')

    # ══════════════ 杂项 ══════════════
    def _clear_log(self):
        self.build_log.clear()
        self.log_text.config(state=NORMAL)
        self.log_text.delete('1.0', END)
        self.log_text.config(state=DISABLED)

    def _copy_log(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append('\n'.join(self.build_log))
            self._log('[日志] 已复制到剪贴板')
        except Exception as exc:
            self._log(f'[错误] {exc}')

    def _open_output(self):
        for path in (self.last_dist_dir,
                     self.output_dir_var.get().strip(),
                     os.path.join(os.path.dirname(os.path.abspath(
                         self.entry_script.get() or SCRIPT_DIR)), 'dist')):
            if path and os.path.isdir(path):
                try:
                    os.startfile(path)
                    return
                except Exception:
                    pass
        messagebox.showinfo('没有输出目录', '还没打包过，也没有指定输出目录。')

    def _show_glossary(self):
        messagebox.showinfo(
            '术语速查',
            'PyInstaller  打包工具\n'
            '-F / --onefile   单文件\n'
            '-D / --onedir    文件夹\n'
            '-w / --noconsole 不弹黑窗\n'
            '--add-data       把资源塞进 exe\n'
            '--hidden-import  强制打包某模块\n'
            '--exclude-module 排除某模块\n'
            '--clean          清缓存\n'
            '--uac-admin      要管理员权限\n'
            '--uac-uiaccess   危险! 未签名程序用了会报「从服务器返回了一个参照」\n'
            '.spec            施工图纸\n'
            'sys._MEIPASS     exe 内部资源目录\n\n'
            '更多 → 标签页「讲解」')

    def _show_troubleshoot(self):
        messagebox.showinfo(
            '常见故障对照表',
            '① 从服务器返回了一个参照\n'
            '   → exe 清单里被写了 uiAccess="true"。\n'
            '     关掉「高级 → --uac-uiaccess」重新打包就行。\n\n'
            '② 双击闪退 / 一闪就没\n'
            '   → 取消「隐藏控制台黑窗」，用控制台模式重打，看报错。\n\n'
            '③ ModuleNotFoundError: No module named xxx\n'
            '   → 模块没打进去。重新检测，选「自动」；\n'
            '     并确认所选解释器里真的装了这个库。\n\n'
            '④ FileNotFoundError (图片/json/音乐)\n'
            '   → 资源没打进去。用目录模式，或勾上「运行时切到资源目录」。\n\n'
            '⑤ exe 体积特别大 (上百 MB)\n'
            '   → 旧版会把注释里的 numpy/pandas 也当依赖。本工具用语法树分析，\n'
            '     只认真实 import；还嫌大就在「高级」里 --exclude-module 排除。\n\n'
            '⑥ 杀毒报毒\n'
            '   → onefile 自解压容易被误判，换文件夹模式或加白名单。')

    def _show_about(self):
        messagebox.showinfo('关于',
                            'Py 脚本打包工具\n周俊宇制作\n\n'
                            'PyInstaller 图形化前端\n'
                            f'运行环境: Python {sys.version.split()[0]}\n'
                            f'配置文件: {CONFIG_PATH}')


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = Tk()
    try:
        root.tk.call('tk', 'scaling', 1.2)
    except Exception:
        pass
    PackerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
