using System.Drawing;
using System.Runtime.InteropServices;
using Chaos.Shell.Core;

namespace Chaos.Shell;

/// <summary>
/// Notification-area icon.
///
/// WinUI 3 has no NotifyIcon, so this is Shell_NotifyIcon over P/Invoke with a
/// message-only window to receive the callbacks. The icon is drawn at runtime
/// from <see cref="TrayState"/> rather than picked from a set of prebuilt
/// assets, because the badge count and the "state unknown" treatment have to
/// reflect live data.
///
/// The distinction that matters: an unreachable platform is drawn hollow with a
/// question mark, never as the calm "no alarms" icon. Silence from the gateway
/// is not evidence of a quiet homestead.
/// </summary>
internal sealed class TrayIcon : IDisposable
{
    private const int WM_APP = 0x8000;
    private const int CallbackMessage = WM_APP + 1;
    private const int WM_LBUTTONUP = 0x0202;
    private const int WM_RBUTTONUP = 0x0205;
    private const int WM_DESTROY = 0x0002;

    private const int NIM_ADD = 0x0;
    private const int NIM_MODIFY = 0x1;
    private const int NIM_DELETE = 0x2;
    private const int NIF_MESSAGE = 0x1;
    private const int NIF_ICON = 0x2;
    private const int NIF_TIP = 0x4;
    private const int NIF_INFO = 0x10;

    private const uint TPM_RIGHTBUTTON = 0x0002;

    private readonly App _app;
    private readonly WndProc _wndProc;
    private IntPtr _hwnd;
    private IntPtr _menu;
    private Icon? _icon;
    private bool _added;
    private TrayState? _last;

    public TrayIcon(App app)
    {
        _app = app;
        _wndProc = WindowProc;
    }

    public void Show()
    {
        _hwnd = CreateMessageWindow();
        BuildMenu();
        Apply(TrayStateFactory.Create(
            LinkStatus.Connecting(), AlarmCounts.None, DateTimeOffset.UtcNow));
    }

    public void Apply(TrayState state)
    {
        if (_hwnd == IntPtr.Zero)
        {
            return;
        }

        // Redraw only when something an operator could see has changed.
        if (_last is not null
            && _last.Icon == state.Icon
            && _last.BadgeText == state.BadgeText
            && _last.Tooltip == state.Tooltip)
        {
            return;
        }
        _last = state;

        var previous = _icon;
        _icon = TrayIconPainter.Draw(state);

        var data = new NOTIFYICONDATA
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATA>(),
            hWnd = _hwnd,
            uID = 1,
            uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage = CallbackMessage,
            hIcon = _icon.Handle,
            szTip = Truncate(state.Tooltip, 127),
        };

        Shell_NotifyIcon(_added ? NIM_MODIFY : NIM_ADD, ref data);
        _added = true;
        previous?.Dispose();
    }

    private static string Truncate(string value, int max) =>
        value.Length <= max ? value : value[..max];

    // ------------------------------------------------------------- window --

    private IntPtr CreateMessageWindow()
    {
        var className = "ChaosShellTray_" + Environment.ProcessId;
        var wc = new WNDCLASS
        {
            lpfnWndProc = Marshal.GetFunctionPointerForDelegate(_wndProc),
            hInstance = GetModuleHandle(null),
            lpszClassName = className,
        };
        RegisterClass(ref wc);

        // HWND_MESSAGE (-3): a window that exists only to receive messages.
        return CreateWindowEx(0, className, className, 0, 0, 0, 0, 0,
            new IntPtr(-3), IntPtr.Zero, wc.hInstance, IntPtr.Zero);
    }

    private IntPtr WindowProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == CallbackMessage)
        {
            var mouse = (int)(lParam.ToInt64() & 0xFFFF);
            if (mouse == WM_LBUTTONUP)
            {
                _app.ShowConsole();
            }
            else if (mouse == WM_RBUTTONUP)
            {
                ShowMenu();
            }
            return IntPtr.Zero;
        }

        if (msg == WM_DESTROY)
        {
            Remove();
        }

        return DefWindowProc(hwnd, msg, wParam, lParam);
    }

    // --------------------------------------------------------------- menu --

    private const int IdConsole = 1;
    private const int IdAnnunciator = 2;
    private const int IdBrowser = 3;
    private const int IdStatus = 4;
    private const int IdSettings = 5;
    private const int IdExit = 9;

    /// <summary>
    /// Rebuilds the menu.
    /// </summary>
    /// <remarks>
    /// The Exit item's wording depends on how the platform is running, so the
    /// menu is rebuilt whenever that changes. The warning has to be on the item
    /// itself: an operator who reads "Exit shell (platform keeps running)" and
    /// clicks it, on a day when this shell IS the platform, has been misled by
    /// the menu before any dialog gets a chance to say otherwise.
    /// </remarks>
    public void RefreshMenu()
    {
        if (_menu != IntPtr.Zero)
        {
            DestroyMenu(_menu);
            _menu = IntPtr.Zero;
        }

        BuildMenu();
    }

    private void BuildMenu()
    {
        _menu = CreatePopupMenu();
        AppendMenu(_menu, 0, IdStatus, "Start screen…");
        AppendMenu(_menu, 0x800, 0, null);            // MF_SEPARATOR
        AppendMenu(_menu, 0, IdConsole, "Open console");
        AppendMenu(_menu, 0, IdAnnunciator, "Open annunciator");
        AppendMenu(_menu, 0, IdBrowser, "Open in browser");
        AppendMenu(_menu, 0x800, 0, null);
        AppendMenu(_menu, 0, IdSettings, "Settings…");
        AppendMenu(_menu, 0x800, 0, null);
        AppendMenu(_menu, 0, IdExit, ShellMessages.ExitMenuItemFor(_app.RunMode));
    }

    private void ShowMenu()
    {
        GetCursorPos(out var pt);

        // Required so the menu dismisses when the user clicks elsewhere.
        SetForegroundWindow(_hwnd);

        var command = TrackPopupMenuEx(
            _menu, TPM_RIGHTBUTTON | 0x0100 /* TPM_RETURNCMD */,
            pt.X, pt.Y, _hwnd, IntPtr.Zero);

        switch (command)
        {
            case IdConsole:
                _app.ShowConsole();
                break;
            case IdAnnunciator:
                _app.ShowAnnunciator();
                break;
            case IdBrowser:
                PlatformController.OpenInBrowser(_app.Endpoints.Console);
                break;
            case IdStatus:
                // The start screen is where platform state lives now: what was
                // found, who owns it, and what can be done about it.
                _app.ShowLauncher();
                break;
            case IdSettings:
                _app.ShowSettings();
                break;
            case IdExit:
                _app.ExitShell();
                break;
        }
    }

    private void Remove()
    {
        if (!_added)
        {
            return;
        }
        var data = new NOTIFYICONDATA
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATA>(),
            hWnd = _hwnd,
            uID = 1,
        };
        Shell_NotifyIcon(NIM_DELETE, ref data);
        _added = false;
    }

    public void Dispose()
    {
        Remove();
        _icon?.Dispose();
        _icon = null;
        if (_menu != IntPtr.Zero)
        {
            DestroyMenu(_menu);
            _menu = IntPtr.Zero;
        }
        if (_hwnd != IntPtr.Zero)
        {
            DestroyWindow(_hwnd);
            _hwnd = IntPtr.Zero;
        }
    }

    // ------------------------------------------------------------ interop --

    private delegate IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct WNDCLASS
    {
        public int style;
        public IntPtr lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpszMenuName;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpszClassName;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NOTIFYICONDATA
    {
        public int cbSize;
        public IntPtr hWnd;
        public int uID;
        public int uFlags;
        public int uCallbackMessage;
        public IntPtr hIcon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string szTip;
        public int dwState;
        public int dwStateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string szInfo;
        public int uVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string szInfoTitle;
        public int dwInfoFlags;
        public Guid guidItem;
        public IntPtr hBalloonIcon;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern bool Shell_NotifyIcon(int message, ref NOTIFYICONDATA data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern ushort RegisterClass(ref WNDCLASS wc);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateWindowEx(
        int exStyle, string className, string windowName, int style,
        int x, int y, int width, int height,
        IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);

    [DllImport("user32.dll")]
    private static extern IntPtr DefWindowProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool DestroyWindow(IntPtr hwnd);

    [DllImport("user32.dll")]
    private static extern IntPtr CreatePopupMenu();

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool AppendMenu(IntPtr menu, int flags, int id, string? item);

    [DllImport("user32.dll")]
    private static extern bool DestroyMenu(IntPtr menu);

    [DllImport("user32.dll")]
    private static extern int TrackPopupMenuEx(
        IntPtr menu, uint flags, int x, int y, IntPtr hwnd, IntPtr parameters);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT point);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hwnd);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandle(string? name);
}
