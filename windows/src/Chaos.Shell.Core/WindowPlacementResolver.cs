namespace Chaos.Shell.Core;

/// <summary>
/// Decides where a window actually opens, given what was saved last time and
/// which displays exist now.
/// </summary>
/// <remarks>
/// <para>
/// The rule: a window is never restored onto a monitor that is no longer
/// attached, and never onto coordinates that are off the visible desktop. On
/// this platform the annunciator commonly lives on a wall display that is
/// switched off, unplugged, or moved between HDMI inputs; a panel restored onto
/// a monitor that is not there is a panel nobody sees.
/// </para>
/// <para>
/// Everything here is arithmetic on plain records so the awkward cases — the
/// wall display gone, the left-hand monitor with negative coordinates, a
/// resolution drop — are testable without a desktop.
/// </para>
/// </remarks>
public static class WindowPlacementResolver
{
    /// <summary>Minimum visible height of the window's top edge, in pixels.</summary>
    /// <remarks>
    /// Enough title bar must remain on-screen to grab with a mouse. A window
    /// technically "on" a monitor but with its caption above the top edge is
    /// unmovable without the keyboard.
    /// </remarks>
    private const int MinimumVisibleCaption = 24;

    /// <summary>Fraction of the window that must overlap a display to count.</summary>
    private const double MinimumVisibleFraction = 0.30;

    public static PlacementResolution Resolve(
        WindowPlacement? saved,
        IReadOnlyList<MonitorInfo> monitors,
        ScreenRect defaultSize,
        int minimumWidth = 640,
        int minimumHeight = 480)
    {
        ArgumentNullException.ThrowIfNull(monitors);

        if (monitors.Count == 0)
        {
            // No displays reported at all. Hand back the request unchanged and
            // let the window manager cope; inventing a monitor would be worse.
            var fallback = new MonitorInfo("(none)", defaultSize, IsPrimary: true, Index: 1);
            return new PlacementResolution(
                defaultSize, fallback, Maximized: false, PlacementAdjustment.Defaulted);
        }

        var primary = monitors.FirstOrDefault(m => m.IsPrimary) ?? monitors[0];

        if (saved is null || saved.Bounds.IsEmpty)
        {
            return new PlacementResolution(
                Center(defaultSize, primary.WorkArea),
                primary,
                Maximized: false,
                PlacementAdjustment.Defaulted);
        }

        var bounds = saved.Bounds;
        var adjustment = PlacementAdjustment.None;

        // A window smaller than its own chrome is not recoverable by dragging.
        if (bounds.Width < minimumWidth || bounds.Height < minimumHeight)
        {
            bounds = bounds with
            {
                Width = Math.Max(bounds.Width, minimumWidth),
                Height = Math.Max(bounds.Height, minimumHeight),
            };
            adjustment = PlacementAdjustment.ResizedToMinimum;
        }

        var namedMonitor = FindByDeviceId(monitors, saved.MonitorDeviceId);
        var overlapMonitor = BestOverlap(monitors, bounds);

        MonitorInfo target;
        if (namedMonitor is not null && IsSufficientlyVisible(bounds, namedMonitor.WorkArea))
        {
            // Saved display is still attached and still shows the window.
            target = namedMonitor;
        }
        else if (namedMonitor is null && saved.MonitorDeviceId is not null)
        {
            // The display it was on has been detached. Do not reuse its
            // coordinates: relocate to whatever is closest, else the primary.
            target = overlapMonitor ?? primary;
            adjustment = PlacementAdjustment.MonitorMissing;
        }
        else if (overlapMonitor is not null)
        {
            // Same display still there but the window no longer sits on it
            // usefully (resolution change, taskbar move), or no display was
            // ever recorded.
            target = overlapMonitor;
            if (adjustment == PlacementAdjustment.None && !IsSufficientlyVisible(bounds, target.WorkArea))
            {
                adjustment = PlacementAdjustment.ClampedToWorkArea;
            }
        }
        else
        {
            // Entirely off the desktop: a monitor was removed, or the layout
            // changed under a saved negative offset.
            target = primary;
            adjustment = saved.MonitorDeviceId is not null
                ? PlacementAdjustment.MonitorMissing
                : PlacementAdjustment.ClampedToWorkArea;
        }

        var placed = ClampInto(bounds, target.WorkArea);
        if (placed != bounds && adjustment == PlacementAdjustment.None)
        {
            adjustment = PlacementAdjustment.ClampedToWorkArea;
        }

        return new PlacementResolution(placed, target, saved.Maximized, adjustment);
    }

    /// <summary>
    /// Picks the display for an explicit request: an index ("2"), a device name,
    /// "primary", or nothing. An out-of-range or unknown request falls back to
    /// the primary display rather than failing — a wall panel that opens on the
    /// wrong screen is fixable, one that does not open is not.
    /// </summary>
    public static MonitorInfo Select(IReadOnlyList<MonitorInfo> monitors, string? request)
    {
        ArgumentNullException.ThrowIfNull(monitors);

        if (monitors.Count == 0)
        {
            throw new ArgumentException("At least one monitor is required.", nameof(monitors));
        }

        var primary = monitors.FirstOrDefault(m => m.IsPrimary) ?? monitors[0];

        var text = request?.Trim();
        if (string.IsNullOrEmpty(text))
        {
            return primary;
        }

        if (string.Equals(text, "primary", StringComparison.OrdinalIgnoreCase))
        {
            return primary;
        }

        if (int.TryParse(text, out var index))
        {
            return monitors.FirstOrDefault(m => m.Index == index) ?? primary;
        }

        return FindByDeviceId(monitors, text) ?? primary;
    }

    private static MonitorInfo? FindByDeviceId(IReadOnlyList<MonitorInfo> monitors, string? deviceId)
    {
        if (string.IsNullOrWhiteSpace(deviceId))
        {
            return null;
        }

        return monitors.FirstOrDefault(
            m => string.Equals(m.DeviceId, deviceId, StringComparison.OrdinalIgnoreCase));
    }

    private static MonitorInfo? BestOverlap(IReadOnlyList<MonitorInfo> monitors, ScreenRect bounds)
    {
        MonitorInfo? best = null;
        long bestArea = 0;

        foreach (var monitor in monitors)
        {
            var area = monitor.WorkArea.IntersectionArea(bounds);
            if (area > bestArea)
            {
                bestArea = area;
                best = monitor;
            }
        }

        return best;
    }

    private static bool IsSufficientlyVisible(ScreenRect bounds, ScreenRect workArea)
    {
        var area = (long)bounds.Width * bounds.Height;
        if (area <= 0)
        {
            return false;
        }

        if (workArea.IntersectionArea(bounds) < (long)(area * MinimumVisibleFraction))
        {
            return false;
        }

        // The caption has to be reachable, not merely on-screen somewhere.
        return bounds.Top >= workArea.Top
            && bounds.Top + MinimumVisibleCaption <= workArea.Bottom;
    }

    /// <summary>
    /// Fits a rectangle inside a work area: shrink if it is too big, then slide
    /// it in. Shrinking first means the result is always fully visible.
    /// </summary>
    private static ScreenRect ClampInto(ScreenRect bounds, ScreenRect workArea)
    {
        var width = Math.Min(bounds.Width, workArea.Width);
        var height = Math.Min(bounds.Height, workArea.Height);

        var left = Math.Clamp(bounds.Left, workArea.Left, workArea.Right - width);
        var top = Math.Clamp(bounds.Top, workArea.Top, workArea.Bottom - height);

        return new ScreenRect(left, top, width, height);
    }

    private static ScreenRect Center(ScreenRect size, ScreenRect workArea)
    {
        var width = Math.Min(size.Width, workArea.Width);
        var height = Math.Min(size.Height, workArea.Height);

        return new ScreenRect(
            workArea.Left + ((workArea.Width - width) / 2),
            workArea.Top + ((workArea.Height - height) / 2),
            width,
            height);
    }
}
