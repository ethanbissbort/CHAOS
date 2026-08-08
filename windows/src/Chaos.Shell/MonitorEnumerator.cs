using System.Runtime.InteropServices;
using Chaos.Shell.Core;

namespace Chaos.Shell;

/// <summary>
/// Enumerates the monitors currently attached, so window placement can be
/// resolved against reality rather than against what was attached last time.
///
/// The decision logic lives in Chaos.Shell.Core's WindowPlacementResolver and
/// is tested there; this file only supplies the facts.
/// </summary>
internal static class MonitorEnumerator
{
    public static IReadOnlyList<MonitorInfo> Current()
    {
        var found = new List<MonitorInfo>();

        // 1-based, as MonitorInfo.Index documents and WindowPlacementResolver.
        // Select relies on: "--monitor 2" and "Display 2" in Settings have to
        // mean the second display, not the third.
        var index = 0;

        bool Callback(IntPtr monitor, IntPtr _, ref RECT __, IntPtr ___)
        {
            var info = new MONITORINFOEX { cbSize = Marshal.SizeOf<MONITORINFOEX>() };
            if (GetMonitorInfo(monitor, ref info))
            {
                index++;
                var work = info.rcWork;
                found.Add(new MonitorInfo(
                    DeviceId: string.IsNullOrEmpty(info.szDevice) ? $"monitor-{index}" : info.szDevice,
                    WorkArea: ScreenRect.FromEdges(work.left, work.top, work.right, work.bottom),
                    IsPrimary: (info.dwFlags & MONITORINFOF_PRIMARY) != 0,
                    Index: index));
            }
            return true;
        }

        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, Callback, IntPtr.Zero);

        // An empty list would make the resolver think every saved monitor is
        // gone. If enumeration fails, say nothing rather than something wrong.
        return found;
    }

    private const int MONITORINFOF_PRIMARY = 0x1;

    private delegate bool MonitorEnumProc(IntPtr monitor, IntPtr dc, ref RECT rect, IntPtr data);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int left;
        public int top;
        public int right;
        public int bottom;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct MONITORINFOEX
    {
        public int cbSize;
        public RECT rcMonitor;
        public RECT rcWork;
        public int dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szDevice;
    }

    [DllImport("user32.dll")]
    private static extern bool EnumDisplayMonitors(
        IntPtr dc, IntPtr clip, MonitorEnumProc callback, IntPtr data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetMonitorInfo(IntPtr monitor, ref MONITORINFOEX info);
}

/// <summary>
/// Keeps the display awake while the annunciator is presented full-screen.
/// A wall panel that blanks after ten minutes is not an alarm panel.
/// </summary>
internal static class DisplayGuard
{
    [Flags]
    private enum ExecutionState : uint
    {
        Continuous = 0x80000000,
        DisplayRequired = 0x00000002,
        SystemRequired = 0x00000001,
    }

    public static void KeepAwake(bool on)
    {
        _ = SetThreadExecutionState(on
            ? ExecutionState.Continuous | ExecutionState.DisplayRequired | ExecutionState.SystemRequired
            : ExecutionState.Continuous);
    }

    [DllImport("kernel32.dll")]
    private static extern uint SetThreadExecutionState(ExecutionState flags);
}
