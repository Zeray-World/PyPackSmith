# -*- coding: utf-8 -*-
"""
light_stub.py —— 轻量启动器（不打包 Python）
═══════════════════════════════════════════════════════════════════
它做的事，和 PyInstaller 完全不同：

    PyInstaller : 把 Python 解释器 + 标准库 + 你的代码 全塞进一个 exe
                  → 34MB 起步，但目标机什么都不用装
    本模块      : 只编一个 ~15KB 的引导器，把你的 .py 当资源嵌进去
                  → 目标机必须自己装好 Python 和用到的库

━━ 原理 ━━
  用 Windows 自带的 C# 编译器（%WINDIR%\\Microsoft.NET\\Framework64\\
  v4.0.30319\\csc.exe，Win10/11 随 .NET Framework 4.x 一起来）现场编一个
  .NET 引导器。它启动时：

    1. 把内嵌的脚本(+数据文件)解到 %LOCALAPPDATA%\\<名字>_lite\\<内容hash>\\
    2. 按顺序找本机的 Python：
         PYPACKER_PYTHON 环境变量 → 构建时的解释器 → 注册表 PythonCore
         → PATH → 常见安装目录
    3. 用 pythonw.exe 无窗执行脚本（控制台模式则用 python.exe）
    4. 找不到 Python / 脚本报缺库 → 弹中文提示，把 pip install 命令都写出来

━━ 降级设计 ━━
  本文件是独立的，主程序 PY脚本打包器.py 通过「可选导入」使用它。
  把这个文件删掉，主程序照常启动，只是「模式5 轻量启动器」会显示不可用。
  反过来本文件也不依赖主程序的任何东西，可以单独 import 使用。

━━ 对外接口 ━━
  available()  -> (bool, 说明文字)          检查编译器在不在
  build(...)   -> (exe路径 or None, 错误 or None)
  guess_name() -> 'xxx-需PY'                按规则生成默认名字
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

SUFFIX = '-需PY'
TAG = '轻量启动器'

# ══════════════════════════════════════════════════════════════
#  编译器定位
# ══════════════════════════════════════════════════════════════
def _dotnet_roots():
    """只认 .NET Framework 4.x。

    模板里用了 RegistryView / RegistryKey.OpenBaseKey，这两个是 4.0 才有的。
    以前把 v3.5 / v2.0 也列进候选，那些机器上编出来是 CS0246 一堆 C# 报错，
    用户根本看不出真正原因是"缺 .NET 4"。
    """
    windir = os.environ.get('WINDIR') or r'C:\Windows'
    out = []
    for arch in ('Framework64', 'Framework'):
        base = os.path.join(windir, 'Microsoft.NET', arch)
        for ver in ('v4.0.30319',):
            out.append(os.path.join(base, ver))
    return out


def find_csc():
    """返回可用的 C# 编译器路径，找不到返回 None。64 位优先。"""
    for folder in _dotnet_roots():
        exe = os.path.join(folder, 'csc.exe')
        if os.path.isfile(exe):
            return exe
    return None


def dotnet_version():
    """.NET Framework 4.x 的发行号，用来给用户看个准信"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full') as k:
            release = winreg.QueryValueEx(k, 'Release')[0]
            version = winreg.QueryValueEx(k, 'Version')[0]
        return f'{version} (Release {release})'
    except Exception:
        return ''


def available():
    """(能用吗, 给界面看的一句话)"""
    if os.name != 'nt':
        return False, '轻量模式只能在 Windows 上用'
    csc = find_csc()
    if not csc:
        return False, '找不到 C# 编译器 csc.exe（需要 .NET Framework 4.x）'
    ver = dotnet_version()
    return True, f'可用 · {ver or ".NET Framework 4.x"} · {os.path.basename(os.path.dirname(os.path.dirname(csc)))}'


def guess_name(script_path):
    """默认名字：脚本名 + -需PY"""
    stem = os.path.splitext(os.path.basename(script_path))[0] or 'app'
    return _safe_name(stem) + SUFFIX


def _safe_name(name):
    bad = '\\/:*?"<>|\r\n\t'
    for ch in bad:
        name = name.replace(ch, '_')
    return name.strip() or 'app'


def _safe_cs(text):
    return (str(text).replace('\\', '\\\\').replace('"', '\\"')
            .replace('\r', ' ').replace('\n', ' '))


# ══════════════════════════════════════════════════════════════
#  C# 引导器模板
#  ★ 必须是 raw string：里面的 \n \r 是写给 C# 的转义，不能被 Python 吃掉
# ══════════════════════════════════════════════════════════════
CS_TEMPLATE = r'''using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32;

[assembly: AssemblyTitle("__TITLE__")]
[assembly: AssemblyProduct("__PRODUCT__")]
[assembly: AssemblyDescription("Python 轻量启动器（不含解释器，依赖本机 Python）")]
[assembly: AssemblyCompany("__COMPANY__")]
[assembly: AssemblyCopyright("")]
[assembly: AssemblyVersion("__VER__")]
[assembly: AssemblyFileVersion("__VER__")]

internal static class LightLauncher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    private const uint MB_OK = 0x00000000;
    private const uint MB_ICONERROR = 0x00000010;
    private const uint MB_TOPMOST = 0x00040000;

    private const string APP_NAME    = "__APP__";
    private const string CACHE_DIR   = "__CACHE_DIR__";
    private const string CACHE_KEY   = "__CACHE_KEY__";
    private const string SCRIPT_RES  = "__SCRIPT_RES__";
    private const string SCRIPT_NAME = "__SCRIPT_NAME__";
    private const string BUILD_PY    = "__BUILD_PY__";
    private const string HINT_MODS   = "__HINT_MODS__";
    private const bool   WANT_CONSOLE = __WANT_CONSOLE__;

    private static readonly string[] DATA_RES  = new string[] { __DATA_RES__ };
    private static readonly string[] DATA_PATH = new string[] { __DATA_PATH__ };

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            return Run(args);
        }
        catch (Exception ex)
        {
            Show("启动器内部错误\n\n" + ex.Message, true);
            return 3;
        }
    }

    private static int Run(string[] args)
    {
        string work = PrepareWorkDir();
        if (!ExtractToString(SCRIPT_RES, Path.Combine(work, SCRIPT_NAME), SCRIPT_NAME))
        {
            Show("启动器解包失败：写不出脚本文件。\n\n"
               + "缓存目录:\n  " + CACHE_DIR + "\n\n"
               + "常见原因: 那个目录没有写权限 / 磁盘满了 / 被杀毒软件锁住。\n"
               + "可以把它删掉再试一次。", true);
            return 4;
        }
        for (int i = 0; i < DATA_RES.Length; i++)
        {
            string rel = DATA_PATH[i].Replace('/', Path.DirectorySeparatorChar);
            ExtractToString(DATA_RES[i], Path.Combine(work, rel), rel);
        }

        string python = FindPython();
        if (python == null)
        {
            Show("这台电脑上没找到 Python。\n\n"
               + "「" + APP_NAME + "」是轻量版，本身不含 Python 解释器，\n"
               + "需要目标电脑自己装好 Python 才能运行。\n\n"
               + "怎么解决：装一个 Python 3（安装时勾选 Add Python to PATH），\n"
               + "装好后再双击本程序即可。"
               + ModuleHint("", true), true);
            return 2;
        }

        string interpreter = PickInterpreter(python, WANT_CONSOLE);
        StringBuilder cmdline = new StringBuilder();
        cmdline.Append(Quote(Path.Combine(work, SCRIPT_NAME)));
        for (int i = 0; i < args.Length; i++)
        {
            cmdline.Append(' ');
            cmdline.Append(Quote(args[i]));
        }

        ProcessStartInfo psi = new ProcessStartInfo(interpreter, cmdline.ToString());
        psi.UseShellExecute = false;
        // CREATE_NO_WINDOW 会把子进程的控制台一起摘掉：控制台模式下 print/报错
        // 全都看不见，只留一个一闪而过的黑窗。控制台模式必须让它继承父控制台。
        psi.CreateNoWindow = !WANT_CONSOLE;
        psi.WorkingDirectory = work;
        try
        {
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        }
        catch (Exception) { }

        bool capture = !WANT_CONSOLE;
        if (capture)
        {
            psi.RedirectStandardError = true;
            psi.RedirectStandardOutput = true;
        }

        Process p = new Process();
        p.StartInfo = psi;
        StringBuilder stderr = new StringBuilder();
        StringBuilder stdout = new StringBuilder();
        if (capture)
        {
            p.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e)
            {
                if (e.Data != null && stderr.Length < 12000) stderr.AppendLine(e.Data);
            };
            p.OutputDataReceived += delegate(object s, DataReceivedEventArgs e)
            {
                if (e.Data != null && stdout.Length < 8000) stdout.AppendLine(e.Data);
            };
        }

        try
        {
            p.Start();
        }
        catch (Exception ex)
        {
            Show("调用 Python 失败：" + ex.Message + "\n\n解释器：" + interpreter, true);
            return 3;
        }

        if (capture)
        {
            p.BeginErrorReadLine();
            p.BeginOutputReadLine();
        }
        p.WaitForExit();
        if (capture)
        {
            try { p.WaitForExit(3000); } catch (Exception) { }
        }
        int code = p.ExitCode;

        if (code != 0 && capture)
        {
            string detail = stderr.Length > 0 ? stderr.ToString() : stdout.ToString();
            string log = Path.Combine(work, "last_error.log");
            try
            {
                File.WriteAllText(log, "STDERR\n" + stderr.ToString()
                                    + "\nSTDOUT\n" + stdout.ToString(), Encoding.UTF8);
            }
            catch (Exception) { }
            Show("「" + APP_NAME + "」运行出错（退出码 " + code + "）。\n\n"
               + Tail(detail, 1100)
               + ModuleHint(detail, false)
               + "\n\n完整日志：" + log, true);
        }
        else if (code != 0)
        {
            Console.WriteLine("[" + APP_NAME + "] 退出码 " + code);
        }
        return code;
    }

    private static string PrepareWorkDir()
    {
        string root = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (String.IsNullOrEmpty(root)) root = Path.GetTempPath();
        string dir = Path.Combine(Path.Combine(root, CACHE_DIR), CACHE_KEY);
        try
        {
            Directory.CreateDirectory(dir);
            return dir;
        }
        catch (Exception) { }

        dir = Path.Combine(Path.Combine(Path.GetTempPath(), CACHE_DIR), CACHE_KEY);
        Directory.CreateDirectory(dir);
        return dir;
    }

    private static bool ExtractToString(string res, string dest, string label)
    {
        Stream src = null;
        try
        {
            src = Assembly.GetExecutingAssembly().GetManifestResourceStream(res);
            if (src == null) return false;

            string parent = Path.GetDirectoryName(dest);
            if (!String.IsNullOrEmpty(parent)) Directory.CreateDirectory(parent);

            // 固定的 dest+".tmp" 在两个实例同时首次启动时会互相抢锁，
            // 后到的那次被 IOException 吞掉，然后去跑一个还不存在的脚本。
            string tmp = dest + "." + Process.GetCurrentProcess().Id
                       + "." + Guid.NewGuid().ToString("N").Substring(0, 8) + ".tmp";
            byte[] buf = new byte[81920];
            using (FileStream fs = new FileStream(tmp, FileMode.Create, FileAccess.Write))
            {
                int n;
                while ((n = src.Read(buf, 0, buf.Length)) > 0) fs.Write(buf, 0, n);
            }
            src.Close();
            src = null;

            if (File.Exists(dest)) File.Delete(dest);
            File.Move(tmp, dest);
            return File.Exists(dest);
        }
        catch (Exception)
        {
            try { if (src != null) src.Close(); } catch (Exception) { }
            return false;
        }
    }

    private static string FindPython()
    {
        string env = Environment.GetEnvironmentVariable("PYPACKER_PYTHON");
        if (!String.IsNullOrEmpty(env) && File.Exists(env)) return env;

        if (!String.IsNullOrEmpty(BUILD_PY) && File.Exists(BUILD_PY)) return BUILD_PY;

        string found = FromRegistry();
        if (found != null) return found;

        found = FromPathEnv();
        if (found != null) return found;

        return FromCommonDirs();
    }

    private static string FromRegistry()
    {
        RegistryHive[] hives = new RegistryHive[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine };
        RegistryView[] views = new RegistryView[] { RegistryView.Registry64, RegistryView.Registry32 };
        for (int h = 0; h < hives.Length; h++)
        {
            for (int v = 0; v < views.Length; v++)
            {
                try
                {
                    using (RegistryKey baseKey = RegistryKey.OpenBaseKey(hives[h], views[v]))
                    {
                        if (baseKey == null) continue;
                        using (RegistryKey core = baseKey.OpenSubKey("Software\\Python\\PythonCore"))
                        {
                            if (core == null) continue;
                            string[] names = core.GetSubKeyNames();
                            // 按版本号数值排，不能按字典序："3.9" 会排在 "3.13" 前面，
                            // 于是一台同时装了 3.13 和 3.9 的机器永远选中旧的 3.9。
                            Array.Sort(names, delegate(string a, string b)
                            {
                                return CompareVer(b, a);
                            });
                            for (int i = 0; i < names.Length; i++)
                            {
                                if (names[i].StartsWith("2.")) continue;
                                using (RegistryKey ip = core.OpenSubKey(names[i] + "\\InstallPath"))
                                {
                                    if (ip == null) continue;
                                    string dir = ip.GetValue("") as string;
                                    string exe = ip.GetValue("ExecutablePath") as string;
                                    if (String.IsNullOrEmpty(exe) && !String.IsNullOrEmpty(dir))
                                        exe = Path.Combine(dir, "python.exe");
                                    if (!String.IsNullOrEmpty(exe) && File.Exists(exe)) return exe;
                                }
                            }
                        }
                    }
                }
                catch (Exception) { }
            }
        }
        return null;
    }

    private static int CompareVer(string a, string b)
    {
        string[] x = a.Split('.');
        string[] y = b.Split('.');
        int n = Math.Max(x.Length, y.Length);
        for (int i = 0; i < n; i++)
        {
            int xi = 0, yi = 0;
            if (i < x.Length) int.TryParse(x[i], out xi);
            if (i < y.Length) int.TryParse(y[i], out yi);
            if (xi != yi) return xi < yi ? -1 : 1;
        }
        return 0;
    }

    private static string FromPathEnv()
    {
        string path = Environment.GetEnvironmentVariable("PATH");
        if (String.IsNullOrEmpty(path)) return null;
        string[] parts = path.Split(';');
        for (int i = 0; i < parts.Length; i++)
        {
            string d = parts[i].Trim().Trim('"');
            if (d.Length == 0) continue;
            if (d.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) >= 0) continue;
            try
            {
                string[] names = new string[] { "python.exe", "python3.exe" };
                for (int k = 0; k < names.Length; k++)
                {
                    string cand = Path.Combine(d, names[k]);
                    if (!File.Exists(cand)) continue;
                    // 微软商店的 python.exe 是 0 字节的 AppExecLink 别名，
                    // File.Exists 为 true 但一运行就弹商店，"没找到 Python"的
                    // 友好提示永远弹不出来。
                    FileInfo fi = new FileInfo(cand);
                    if (fi.Length == 0) continue;
                    return cand;
                }
            }
            catch (Exception) { }
        }
        return null;
    }

    private static string FromCommonDirs()
    {
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        string pf = Environment.GetEnvironmentVariable("ProgramFiles");
        string pf86 = Environment.GetEnvironmentVariable("ProgramFiles(x86)");
        string[] bases = new string[]
        {
            local == null ? null : Path.Combine(local, "Programs\\Python"),
            local == null ? null : Path.Combine(local, "Programs"),
            pf,
            pf86,
            "C:\\"
        };

        for (int b = 0; b < bases.Length; b++)
        {
            if (String.IsNullOrEmpty(bases[b])) continue;
            try
            {
                if (!Directory.Exists(bases[b])) continue;
                string[] dirs = Directory.GetDirectories(bases[b], "Python*");
                for (int i = 0; i < dirs.Length; i++)
                {
                    string cand = Path.Combine(dirs[i], "python.exe");
                    if (File.Exists(cand)) return cand;
                }
            }
            catch (Exception) { }
        }
        return null;
    }

    private static string PickInterpreter(string pythonExe, bool console)
    {
        if (!console)
        {
            try
            {
                string w = Path.Combine(Path.GetDirectoryName(pythonExe), "pythonw.exe");
                if (File.Exists(w)) return w;
            }
            catch (Exception) { }
        }
        return pythonExe;
    }

    private static string Quote(string s)
    {
        if (s == null) return "\"\"";
        string q = s.Replace("\"", "\\\"");
        // 结尾是奇数个反斜杠时，"C:\dir\" 里的 \" 会被当成转义引号，
        // 闭合引号被吃掉，参数变成 C:\dir" 并把后面的参数一起吞进来。
        // 资源管理器把文件夹拖到 exe 上时传的路径经常带尾反斜杠。
        int bs = 0;
        for (int i = q.Length - 1; i >= 0 && q[i] == '\\'; i--) bs++;
        if (bs % 2 == 1) q += "\\";
        return "\"" + q + "\"";
    }

    private static string Tail(string s, int max)
    {
        if (String.IsNullOrEmpty(s)) return "（Python 没有输出错误信息）";
        if (s.Length <= max) return s;
        return "..." + s.Substring(s.Length - max);
    }

    private static string ModuleHint(string err, bool noPython)
    {
        StringBuilder sb = new StringBuilder();
        if (!String.IsNullOrEmpty(err))
        {
            int i = err.IndexOf("No module named");
            if (i >= 0)
            {
                string rest = err.Substring(i + 15).Trim().Trim('\'', '"', ' ', ':');
                int end = rest.IndexOfAny(new char[] { '\r', '\n', ' ', '\'', '"' });
                if (end > 0) rest = rest.Substring(0, end);
                if (rest.Length > 0 && rest.Length < 80)
                {
                    sb.Append("\n\n缺少 Python 库：" + rest);
                    sb.Append("\n在目标电脑上执行：pip install " + rest);
                }
            }
        }
        if (!String.IsNullOrEmpty(HINT_MODS))
        {
            sb.Append("\n\n打包时检测到这些库，目标机同样要装：");
            string[] mods = HINT_MODS.Split('|');
            for (int i = 0; i < mods.Length; i++)
            {
                if (mods[i].Length == 0) continue;
                sb.Append("\n  pip install " + mods[i]);
            }
        }
        return sb.ToString();
    }

    private static void Show(string text, bool isError)
    {
        uint flags = MB_OK | MB_TOPMOST | (isError ? MB_ICONERROR : 0);
        try
        {
            MessageBoxW(IntPtr.Zero, text, APP_NAME, flags);
        }
        catch (Exception)
        {
            Console.WriteLine(text);
        }
    }
}
'''

MANIFEST_TEMPLATE = '''<?xml version="1.0" encoding="utf-8"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="__LEVEL__" uiAccess="false" />
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}" />
      <supportedOS Id="{35138b9a-5d96-4fbd-8e2d-a2440225f93a}" />
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true</dpiAware>
    </windowsSettings>
  </application>
</assembly>
'''


# ══════════════════════════════════════════════════════════════
#  工具
# ══════════════════════════════════════════════════════════════
def _clean_env():
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE'):
        env.pop(key, None)
    return env


def _run(cmd, timeout=300):
    """跑编译器。csc 的错误信息是系统本地代码页，先试 utf-8 再退回 gbk。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=NO_WINDOW, env=_clean_env())
    except FileNotFoundError as exc:
        return -1, f'找不到编译器: {exc}'
    except subprocess.TimeoutExpired:
        return -9, '编译超时'
    except Exception as exc:
        return -1, str(exc)

    raw = (proc.stdout or b'') + (proc.stderr or b'')
    text = raw.decode('utf-8', errors='replace')
    if '\ufffd' in text:
        try:
            text = raw.decode('gbk', errors='replace')
        except Exception:
            pass
    return proc.returncode, text


def _norm_version(version):
    """规范化成 4 段版本号。每段必须 ≤65534，否则 csc 报 CS7034，
    用户看到的是"C# 编译失败"，跟"版本号写大了"完全对不上。"""
    parts = []
    for chunk in str(version or '').replace(',', '.').split('.')[:4]:
        num = ''.join(ch for ch in chunk if ch.isdigit()) or '0'
        try:
            parts.append(str(min(int(num), 65534)))
        except (TypeError, ValueError):
            parts.append('0')
    while len(parts) < 4:
        parts.append('0')
    return '.'.join(parts)


def _make_workdir(log):
    """建构建用的临时目录。

    csc 的 /resource:file,id 是按逗号切分的，工作目录路径里只要有一个逗号
    （AD 里 "Smith, John" 这种用户名很常见），整个编译就失败，而且报错
    跟真实原因毫无关系。所以优先挑一个不含逗号的落脚点。
    """
    candidates = [tempfile.gettempdir(),
                  os.environ.get('ProgramData'),
                  os.path.join(os.environ.get('SystemDrive', 'C:') + os.sep, 'Temp')]
    for base in candidates:
        if not base or ',' in base or not os.path.isdir(base):
            continue
        try:
            return tempfile.mkdtemp(prefix='pypacker_lite_', dir=base)
        except OSError:
            continue
    raise RuntimeError('找不到一个路径里不含逗号的临时目录（编译器的 /resource 参数用逗号分隔）。'
                       '请把系统 TEMP 指到一个不含逗号的路径。')


def _collect_data(items, workdir, log, reserved=()):
    """把数据文件铺到 workdir/data/ 下，返回 [(资源名, 相对路径)]

    reserved: 已经被占用的相对路径（入口脚本名），重名的数据文件必须改名，
    否则解包时后写的会把脚本覆盖掉 —— 启动器于是去执行一个 json。
    """
    rows = []
    if not items:
        return rows
    used = {r.lower() for r in reserved if r}
    index = 0

    def unique_rel(rel):
        if rel.lower() not in used:
            used.add(rel.lower())
            return rel
        stem, ext = os.path.splitext(rel)
        n = 2
        while f'{stem}_{n}{ext}'.lower() in used:
            n += 1
        rel = f'{stem}_{n}{ext}'
        used.add(rel.lower())
        log(f'[轻量] 数据文件重名，改名后嵌入: {rel}')
        return rel

    for src in items:
        src = os.path.abspath(src)
        if not os.path.exists(src):
            log(f'[轻量] 跳过不存在的数据: {src}')
            continue
        if os.path.isfile(src):
            rel = unique_rel(os.path.basename(src))
            dest = os.path.join(workdir, 'data', str(index))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            rows.append((f'res{index}', rel))
            index += 1
        else:
            base_name = os.path.basename(src.rstrip('\\/')) or 'data'
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d != '__pycache__']
                for name in files:
                    full = os.path.join(root, name)
                    rel_inside = os.path.relpath(full, src).replace('\\', '/')
                    rel = unique_rel(base_name + '/' + rel_inside)
                    dest = os.path.join(workdir, 'data', str(index))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(full, dest)
                    rows.append((f'res{index}', rel))
                    index += 1
    return rows


# ══════════════════════════════════════════════════════════════
#  构建
# ══════════════════════════════════════════════════════════════
def build(entry_py, out_dir, name, icon=None, console=False, uac_admin=False,
          version='1.0.0.0', data_files=None, modules=None, build_python=None,
          company='', log=None):
    """
    返回 (exe 路径, None) 或 (None, 错误说明)
    """
    log = log or (lambda s: None)

    if os.name != 'nt':
        return None, '轻量模式只支持 Windows'

    # 这几个参数是必填的，空着就没法干活。抛带说明的异常，不往下走到
    # os.path.abspath(None) 那种地方崩一个看不懂的 TypeError。
    if not isinstance(entry_py, str) or not entry_py.strip():
        raise ValueError('light_stub.build() 需要入口脚本路径 entry_py，实际收到的是空值')
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise ValueError('light_stub.build() 需要输出目录 out_dir，实际收到的是空值')
    if not isinstance(name, str):
        raise ValueError('light_stub.build() 的 name 必须是字符串，留空请传空字符串 ""')

    entry_py = os.path.abspath(entry_py)
    if not os.path.isfile(entry_py):
        return None, f'入口脚本不存在: {entry_py}'
    if entry_py.lower().endswith('.spec'):
        return None, '轻量模式不支持 .spec 文件，请选一个 .py'

    csc = find_csc()
    if not csc:
        return None, ('找不到 C# 编译器 csc.exe。\n'
                      'Windows 10/11 自带 .NET Framework 4.x 就有，\n'
                      '检查 C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe')

    name = _safe_name(name)
    out_dir = os.path.abspath(out_dir)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        return None, f'输出目录建不了: {out_dir}\n{exc}'
    final_exe = os.path.join(out_dir, name + '.exe')

    if icon and not os.path.isfile(icon):
        log(f'[轻量] 图标不存在，忽略: {icon}')
        icon = None
    if icon and not icon.lower().endswith('.ico'):
        return None, '图标必须是 .ico 格式'

    workdir = None
    try:
        workdir = _make_workdir(log)
    except Exception as exc:
        return None, f'创建构建临时目录失败: {exc}'
    try:
        # ── 脚本与数据 ──
        with open(entry_py, 'rb') as f:
            script_bytes = f.read()
        script_name = os.path.basename(entry_py)
        if not script_name.lower().endswith('.py'):
            script_name = os.path.splitext(script_name)[0] + '.py'
        script_tmp = os.path.join(workdir, 'script_src.py')
        with open(script_tmp, 'wb') as f:
            f.write(script_bytes)

        data_rows = _collect_data(data_files or [], workdir, log,
                                  reserved=(script_name,))

        # 内容指纹 → 目标机的缓存目录，换了脚本就换目录，不会用到旧的
        digest = hashlib.sha256(script_bytes)
        for res, rel in data_rows:
            digest.update(rel.encode('utf-8'))
            try:
                with open(os.path.join(workdir, 'data', res[3:]), 'rb') as f:
                    digest.update(f.read())
            except OSError:
                pass
        cache_key = digest.hexdigest()[:12]

        # ── C# 源码 ──
        # 提示里只列真正需要 pip 装的：标准库跳过，且按"整项"截断，
        # 不能把包名切成两半。用 | 分隔，C# 侧逐行展开成 pip install xxx。
        stdlib = set(getattr(sys, 'stdlib_module_names', ()) or ())
        picked, used_len = [], 0
        for m in sorted({m.split('.')[0] for m in (modules or []) if m}):
            if not m or m in stdlib:
                continue
            if used_len + len(m) > 168:
                break
            picked.append(m)
            used_len += len(m) + 1
        hint = '|'.join(picked)
        cs = CS_TEMPLATE
        cs = cs.replace('__TITLE__', _safe_cs(name))
        cs = cs.replace('__PRODUCT__', _safe_cs(name))
        cs = cs.replace('__COMPANY__', _safe_cs(company or ''))
        cs = cs.replace('__VER__', _norm_version(version))
        cs = cs.replace('__APP__', _safe_cs(name))
        cs = cs.replace('__CACHE_DIR__', _safe_cs(name + '_lite'))
        cs = cs.replace('__CACHE_KEY__', cache_key)
        cs = cs.replace('__SCRIPT_RES__', 'scriptres')
        cs = cs.replace('__SCRIPT_NAME__', _safe_cs(script_name))
        cs = cs.replace('__BUILD_PY__', _safe_cs(build_python or ''))
        cs = cs.replace('__HINT_MODS__', _safe_cs(hint))
        cs = cs.replace('__WANT_CONSOLE__', 'true' if console else 'false')
        cs = cs.replace('__DATA_RES__',
                        ', '.join('"' + r + '"' for r, _ in data_rows))
        cs = cs.replace('__DATA_PATH__',
                        ', '.join('"' + _safe_cs(p) + '"' for _, p in data_rows))

        cs_path = os.path.join(workdir, 'Launcher.cs')
        with open(cs_path, 'w', encoding='utf-8-sig', newline='\r\n') as f:
            f.write(cs)

        manifest_path = os.path.join(workdir, 'app.manifest')
        with open(manifest_path, 'w', encoding='utf-8') as f:
            f.write(MANIFEST_TEMPLATE.replace(
                '__LEVEL__', 'requireAdministrator' if uac_admin else 'asInvoker'))

        out_tmp = os.path.join(workdir, 'built.exe')
        cmd = [csc, '/nologo', '/platform:anycpu', '/optimize+',
               '/target:' + ('exe' if console else 'winexe'),
               '/out:' + out_tmp,
               '/r:System.dll',
               '/win32manifest:' + manifest_path,
               '/resource:' + script_tmp + ',scriptres']
        if icon:
            cmd.append('/win32icon:' + os.path.abspath(icon))
        for res, _ in data_rows:
            cmd.append('/resource:' +
                       os.path.join(workdir, 'data', res[3:]) + ',' + res)
        cmd.append(cs_path)

        log('[轻量] 编译器: ' + csc)
        log('[轻量] 内嵌数据: %d 个' % len(data_rows))
        log('[轻量] 编译中…')
        code, output = _run(cmd)
        if output.strip():
            for line in output.strip().splitlines()[:40]:
                log('[编译器] ' + line.strip())

        if code != 0 or not os.path.isfile(out_tmp):
            return None, 'C# 编译失败（返回码 %s）\n%s' % (
                code, (output or '没有输出').strip()[:1500])

        # ── 落地 ──
        if os.path.exists(final_exe):
            try:
                os.remove(final_exe)
            except OSError:
                return None, f'旧的 exe 正在运行，先关掉它再打包: {final_exe}'
        shutil.copy2(out_tmp, final_exe)

        size = os.path.getsize(final_exe)
        log(f'[轻量] 完成: {final_exe}  ({size / 1024:.1f} KB)')
        return final_exe, None
    except OSError as exc:
        # 数据文件被占用、磁盘满、目录没权限…… 一律走返回值，别把异常甩给界面
        return None, f'打包过程中读写文件失败: {exc}'
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    ok, msg = available()
    print(TAG, '可用' if ok else '不可用', '-', msg)
    print('默认命名示例:', guess_name(r'C:\demo\爬虫工具.py'))
    csc = find_csc()
    print('编译器:', csc)
