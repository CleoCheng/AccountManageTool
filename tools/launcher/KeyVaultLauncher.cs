// KeyVault.exe - tiny Windows launcher that starts main.py with pythonw.exe (no console window).
// Build: powershell -ExecutionPolicy Bypass -File tools\launcher\build.ps1
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("KeyVault Account Manager")]
[assembly: AssemblyProduct("KeyVault")]
[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]

class Launcher {
    static bool IsValidPythonw(string candidate) {
        if (string.IsNullOrWhiteSpace(candidate) || !File.Exists(candidate)) return false;
        // Skip the Microsoft Store stub, which opens the Store instead of running Python.
        return candidate.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) < 0;
    }

    static int[] ParsePythonVersion(string text) {
        if (string.IsNullOrWhiteSpace(text)) return new int[] { 0, 0, 0 };
        var dotted = Regex.Match(text, @"(\d+)\.(\d+)(?:\.(\d+))?");
        if (dotted.Success) {
            return new int[] {
                int.Parse(dotted.Groups[1].Value),
                int.Parse(dotted.Groups[2].Value),
                dotted.Groups[3].Success ? int.Parse(dotted.Groups[3].Value) : 0
            };
        }
        var compact = Regex.Match(text, @"Python(\d)(\d+)", RegexOptions.IgnoreCase);
        if (compact.Success) {
            return new int[] {
                int.Parse(compact.Groups[1].Value),
                int.Parse(compact.Groups[2].Value),
                0
            };
        }
        return new int[] { 0, 0, 0 };
    }

    static int CompareVersionDesc(int[] a, int[] b) {
        for (int i = 0; i < 3; i++) {
            int cmp = b[i].CompareTo(a[i]);
            if (cmp != 0) return cmp;
        }
        return 0;
    }

    static void CollectRegistryPythonCandidates(List<KeyValuePair<int[], string>> found) {
        RegistryHive[] hives = new RegistryHive[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine };
        RegistryView[] views = new RegistryView[] { RegistryView.Registry64, RegistryView.Registry32 };
        foreach (var hive in hives) {
            foreach (var view in views) {
                try {
                    using (var baseKey = RegistryKey.OpenBaseKey(hive, view))
                    using (var pyCore = baseKey.OpenSubKey(@"Software\Python\PythonCore")) {
                        if (pyCore == null) continue;
                        foreach (string verName in pyCore.GetSubKeyNames()) {
                            try {
                                using (var installKey = pyCore.OpenSubKey(verName + @"\InstallPath")) {
                                    if (installKey == null) continue;
                                    string dir = installKey.GetValue(null) as string;
                                    string windowed = installKey.GetValue("WindowedExecutablePath") as string;
                                    string candidate = !string.IsNullOrWhiteSpace(windowed)
                                        ? windowed
                                        : (!string.IsNullOrWhiteSpace(dir) ? Path.Combine(dir, "pythonw.exe") : "");
                                    if (IsValidPythonw(candidate)) {
                                        found.Add(new KeyValuePair<int[], string>(ParsePythonVersion(verName), candidate));
                                    }
                                }
                            } catch {}
                        }
                    }
                } catch {}
            }
        }
    }

    // Resolution order: project .venv / venv -> pythonw.exe on PATH -> Registry PythonCore -> common folders -> pyw.exe.
    static string FindPythonw(string baseDir) {
        string[] localVenvs = new string[] {
            Path.Combine(baseDir, ".venv", "Scripts", "pythonw.exe"),
            Path.Combine(baseDir, "venv", "Scripts", "pythonw.exe")
        };
        foreach (string p in localVenvs) {
            if (IsValidPythonw(p)) return p;
        }

        string pathEnv = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string dir in pathEnv.Split(Path.PathSeparator)) {
            if (string.IsNullOrWhiteSpace(dir)) continue;
            try {
                string candidate = Path.Combine(dir.Trim(), "pythonw.exe");
                if (IsValidPythonw(candidate)) return candidate;
            } catch {}
        }

        var versioned = new List<KeyValuePair<int[], string>>();
        CollectRegistryPythonCandidates(versioned);

        string[] searchRoots = new string[] {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Python"),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            @"C:\"
        };
        foreach (string root in searchRoots) {
            try {
                if (string.IsNullOrWhiteSpace(root) || !Directory.Exists(root)) continue;
                foreach (string sd in Directory.GetDirectories(root, "Python*")) {
                    string candidate = Path.Combine(sd, "pythonw.exe");
                    if (IsValidPythonw(candidate)) {
                        versioned.Add(new KeyValuePair<int[], string>(ParsePythonVersion(Path.GetFileName(sd)), candidate));
                    }
                }
            } catch {}
        }

        versioned.Sort((a, b) => CompareVersionDesc(a.Key, b.Key));
        if (versioned.Count > 0) return versioned[0].Value;

        string winDir = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        if (!string.IsNullOrWhiteSpace(winDir)) {
            string pywLauncher = Path.Combine(winDir, "pyw.exe");
            if (File.Exists(pywLauncher)) return pywLauncher;
        }
        return "pythonw.exe";
    }

    static string QuoteArg(string arg) {
        if (string.IsNullOrEmpty(arg)) return "\"\"";
        var sb = new StringBuilder();
        sb.Append('"');
        int backslashes = 0;
        foreach (char c in arg) {
            if (c == '\\') {
                backslashes++;
            } else if (c == '"') {
                sb.Append('\\', backslashes * 2 + 1);
                sb.Append('"');
                backslashes = 0;
            } else {
                if (backslashes > 0) {
                    sb.Append('\\', backslashes);
                    backslashes = 0;
                }
                sb.Append(c);
            }
        }
        if (backslashes > 0) sb.Append('\\', backslashes * 2);
        sb.Append('"');
        return sb.ToString();
    }

    [STAThread]
    static void Main(string[] args) {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        string mainPy = Path.Combine(baseDir, "main.py");
        if (!File.Exists(mainPy)) {
            MessageBox.Show("未找到 main.py，请确保 KeyVault.exe 位于项目根目录。", "KeyVault 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        string pyw = FindPythonw(baseDir);
        List<string> argList = new List<string>();
        argList.Add(QuoteArg(mainPy));
        if (args != null) {
            foreach (string a in args) {
                if (!string.IsNullOrEmpty(a)) argList.Add(QuoteArg(a));
            }
        }
        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = pyw;
        psi.Arguments = string.Join(" ", argList.ToArray());
        psi.WorkingDirectory = baseDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        try {
            Process.Start(psi);
        } catch (Exception ex) {
            MessageBox.Show("启动 Python 环境失败: " + ex.Message + "\n请确认已安装 Python 及 requirements.txt 依赖。", "KeyVault 启动错误", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
