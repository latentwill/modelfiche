using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class Program {
    internal static readonly string Bundle = AppDomain.CurrentDomain.BaseDirectory;
    internal static string Support { get { return Environment.GetEnvironmentVariable("TITLES_APP_SUPPORT") ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Modelfiche"); } }
    internal static string Quote(string value) {
        var result = new StringBuilder("\""); int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            result.Append('\\', c == '"' ? slashes * 2 + 1 : slashes); result.Append(c); slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }
    internal static ProcessStartInfo Python(string module, string[] args) {
        var info = new ProcessStartInfo(Path.Combine(Bundle, "runtime", "python.exe"), "-m " + module + " " + string.Join(" ", args.Select(Quote))) { UseShellExecute = false, WorkingDirectory = Bundle };
        info.EnvironmentVariables["MODELFICHE_BUNDLE_ROOT"] = Bundle;
        info.EnvironmentVariables["TITLES_APP_SUPPORT"] = Support;
        info.EnvironmentVariables["PYTHONUTF8"] = "1";
        return info;
    }
    [STAThread] private static int Main(string[] args) {
        Directory.CreateDirectory(Support);
#if CLI
        try { using (var process = Process.Start(Python("titles_cli.windows_cli", args))) { process.WaitForExit(); return process.ExitCode; } }
        catch (Exception error) { Console.Error.WriteLine(error.Message); return 1; }
#else
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new Launcher(args.Contains("--no-browser")));
        return Environment.ExitCode;
#endif
    }
}

internal sealed class Launcher : Form {
    private readonly bool noBrowser;
    private readonly Label status = new Label { AutoSize = true, MaximumSize = new Size(450, 0), Text = "Starting your workspace..." };
    private readonly Button open = new Button { Text = "&Open Modelfiche", AutoSize = true, Enabled = false, Padding = new Padding(10, 5, 10, 5) };
    private readonly Button stop = new Button { Text = "&Stop and close", AutoSize = true, Padding = new Padding(10, 5, 10, 5) };
    private Process process; private Job job; private StreamWriter log;
    private bool closing, finished; private string url;
    internal Launcher(bool noBrowser) {
        this.noBrowser = noBrowser;
        Text = "Modelfiche"; Font = new Font("Segoe UI", 10); BackColor = Color.FromArgb(248, 249, 248);
        AutoScaleMode = AutoScaleMode.Dpi; AutoSize = true; AutoSizeMode = AutoSizeMode.GrowAndShrink;
        FormBorderStyle = FormBorderStyle.FixedSingle; MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        var layout = new TableLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, Padding = new Padding(28), ColumnCount = 1 };
        layout.Controls.Add(new Label { Text = "Modelfiche", Font = new Font("Segoe UI", 22, FontStyle.Bold), AutoSize = true, Margin = new Padding(0, 0, 0, 14) });
        status.Margin = new Padding(0, 0, 0, 10); layout.Controls.Add(status);
        layout.Controls.Add(new Label { Text = "Keep this window open while you work in your browser.\nClosing it stops Modelfiche. Your work stays saved.", AutoSize = true, Margin = new Padding(0, 0, 0, 22) });
        var actions = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new Padding(0) };
        actions.Controls.Add(open); actions.Controls.Add(stop); layout.Controls.Add(actions);
        var logs = new LinkLabel { Text = "View logs", AutoSize = true, Margin = new Padding(0, 18, 0, 0) };
        logs.LinkClicked += (s, e) => { var path = Path.Combine(Program.Support, "logs"); Directory.CreateDirectory(path); Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); };
        layout.Controls.Add(logs); Controls.Add(layout); AcceptButton = open;
        open.Click += (s, e) => OpenBrowser(); stop.Click += (s, e) => Close();
        Shown += async (s, e) => await StartRuntime();
        FormClosing += async (s, e) => {
            if (finished || process == null) return;
            e.Cancel = true; if (closing) return; closing = true;
            open.Enabled = stop.Enabled = false; status.Text = "Stopping Modelfiche...";
            try { process.StandardInput.WriteLine("stop"); process.StandardInput.Flush(); } catch { }
            await Task.Run(() => { try { process.WaitForExit(25000); } catch { } });
            if (job != null) job.Dispose(); finished = true; Close();
        };
    }
    private void OpenBrowser() {
        if (url == null) return;
        try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
        catch { status.Text = "Open " + url + " in your browser."; }
    }
    private async Task StartRuntime() {
        try {
            Directory.CreateDirectory(Path.Combine(Program.Support, "logs"));
            // Each launcher gets its own log so opening the app twice is safe.
            log = new StreamWriter(Path.Combine(Program.Support, "logs", "launcher-" + Process.GetCurrentProcess().Id + ".log"), true) { AutoFlush = true };
            job = new Job();
            var info = Program.Python("titles_cli.desktop", new[] { "--handshake" });
            info.CreateNoWindow = true; info.RedirectStandardOutput = true; info.RedirectStandardError = true; info.RedirectStandardInput = true;
            process = Process.Start(info); job.Add(process);
            process.OutputDataReceived += (s, e) => {
                if (e.Data == null) return;
                if (e.Data.StartsWith("READY ") || e.Data.StartsWith("ALREADY ")) BeginInvoke((Action)(() => {
                    url = e.Data.Substring(e.Data.IndexOf(' ') + 1); status.Text = "Your workspace is running."; open.Enabled = true;
                    var capturePath = Environment.GetEnvironmentVariable("MODELFICHE_LAUNCHER_SCREENSHOT");
                    if (!string.IsNullOrEmpty(capturePath)) {
                        PerformLayout();
                        using (var capture = new Bitmap(Width, Height)) { DrawToBitmap(capture, new Rectangle(0, 0, Width, Height)); capture.Save(capturePath, System.Drawing.Imaging.ImageFormat.Png); }
                    }
                    if (!noBrowser) OpenBrowser();
                }));
            };
            process.ErrorDataReceived += (s, e) => { if (e.Data != null) lock (log) log.WriteLine(e.Data); };
            process.BeginOutputReadLine(); process.BeginErrorReadLine();
            process.StandardInput.WriteLine("start"); process.StandardInput.Flush();
            await Task.Run(() => { process.WaitForExit(); });
            if (process.ExitCode != 0 && !closing) {
                Environment.ExitCode = process.ExitCode; status.Text = "Modelfiche could not keep running. View logs for details, then reopen the app."; open.Enabled = false;
            } else if (!closing) { finished = true; Close(); }
        } catch (Exception error) {
            Environment.ExitCode = 1; status.Text = "Unable to start: " + error.Message;
            if (log != null) log.WriteLine(error);
        } finally { if (job != null) job.Dispose(); if (log != null) log.Dispose(); }
    }
}

// Children cannot survive an exit or crash of the native launcher.
internal sealed class Job : IDisposable {
    [StructLayout(LayoutKind.Sequential)] private struct Basic { public long UserTime, JobTime; public uint Flags; public UIntPtr MinWorkingSet, MaxWorkingSet; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint Priority, Scheduling; }
    [StructLayout(LayoutKind.Sequential)] private struct IO { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] private struct Limits { public Basic Basic; public IO IO; public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(IntPtr job, int kind, ref Limits limits, uint length);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    private IntPtr handle;
    internal Job() {
        handle = CreateJobObject(IntPtr.Zero, null);
        var limits = new Limits(); limits.Basic.Flags = 0x2000;
        if (handle == IntPtr.Zero || !SetInformationJobObject(handle, 9, ref limits, (uint)Marshal.SizeOf(limits))) { Dispose(); throw new System.ComponentModel.Win32Exception(); }
    }
    internal void Add(Process process) { if (!AssignProcessToJobObject(handle, process.Handle)) { process.Kill(); throw new System.ComponentModel.Win32Exception(); } }
    public void Dispose() { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
}
