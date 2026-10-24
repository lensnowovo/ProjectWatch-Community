// Windowless entry point. No shell, package installation or shared Python lookup.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using System.Net;

internal static class Launcher {
    [STAThread]
    private static void Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string data = null;
        FileStream gate = null;
        string stateFile = Path.Combine(root, "install-state.json");
        if (File.Exists(stateFile)) {
            try {
                var state = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(stateFile));
                string active = (string)state["active"];
                if (!System.Text.RegularExpressions.Regex.IsMatch(active, "^[0-9a-f]{64}$")) throw new Exception();
                data = (string)state["home"];
                if (!Path.IsPathRooted(data) || data.Contains("\"")) throw new Exception();
                // Cover interpreter startup as well as the running app. Python's
                // data lock alone is too late to guard imports during uninstall.
                gate = new FileStream(Path.Combine(root, ".studio.lock"), FileMode.OpenOrCreate,
                                      FileAccess.ReadWrite, FileShare.ReadWrite);
                gate.Lock(0, 1);
                root = Path.Combine(root, "versions", active);
            } catch {
                if (gate != null) gate.Dispose();
                if (data != null && OpenExisting(data)) return;
                MessageBox.Show("ProjectWatch 正在启动、退出或安装维护中，请稍后重试。若持续出现，请重新运行安装器。资料保留。", "ProjectWatch");
                return;
            }
        }
        string python = Path.Combine(root, "runtime", "pythonw.exe");
        if (!File.Exists(python)) {
            if (gate != null) gate.Dispose();
            MessageBox.Show("安装文件不完整，请重新安装 ProjectWatch。已有资料不会被删除。", "ProjectWatch");
            return;
        }
        try {
            string smoke = "";
            if (args.Length > 0) {
                int seconds;
                // Developer-only validation mode: explicit, bounded and no workers.
                if (data == null || args.Length != 2 || args[0] != "--smoke-seconds" ||
                    !Int32.TryParse(args[1], out seconds) || seconds < 1 || seconds > 300) throw new Exception();
                smoke = " --headless --smoke-seconds " + seconds;
            }
            var start = new ProcessStartInfo {
                FileName = python,
                Arguments = "-I -B -m project_watch.studio.desktop" + (data == null ? "" : " --home \"" + data.TrimEnd('\\') + "\"" ) + smoke,
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            // The desktop edition chooses its own data home and bundled modules.
            start.EnvironmentVariables.Remove("PYTHONPATH");
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PROJECT_WATCH_HOME");
            using (var child = Process.Start(start)) {
                if (gate != null) child.WaitForExit();
            }
        } catch {
            MessageBox.Show("ProjectWatch 无法启动。请检查安装目录权限，或重新安装；不要删除资料目录。", "ProjectWatch");
        } finally {
            if (gate != null) gate.Dispose();
        }
    }

    private static bool OpenExisting(string home) {
        try {
            var json = new JavaScriptSerializer();
            var marker = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(home, "desktop-session", "endpoint.json")));
            if (!(marker["port"] is int)) return false;
            int port = (int)marker["port"];
            string id = (string)marker["instance"];
            if (port < 1 || port > 65535 || !System.Text.RegularExpressions.Regex.IsMatch(id, "^[0-9a-f]{32}$")) return false;
            string url = "http://127.0.0.1:" + port + "/";
            var request = (HttpWebRequest)WebRequest.Create(url + "api/desktop/identity");
            request.Proxy = null;
            request.AllowAutoRedirect = false;
            request.Timeout = 1500;
            request.ReadWriteTimeout = 1500;
            using (var response = request.GetResponse())
            using (var reader = new StreamReader(response.GetResponseStream())) {
                char[] buffer = new char[4097];
                int count = 0, read;
                while (count < buffer.Length && (read = reader.Read(buffer, count, buffer.Length-count)) > 0) count += read;
                if (count > 4096) return false;
                var reply = json.Deserialize<Dictionary<string, object>>(new string(buffer, 0, count));
                if ((string)reply["instance"] != id) return false;
            }
            Process.Start(url);
            return true;
        } catch { return false; }
    }
}
