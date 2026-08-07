namespace Chaos.Shell.Core;

/// <summary>
/// A rectangle in virtual-screen coordinates. Left and Top are signed: a
/// monitor placed to the left of the primary has negative X, which is a normal
/// multi-monitor layout and must survive a round trip through persistence.
/// </summary>
public readonly record struct ScreenRect(int Left, int Top, int Width, int Height)
{
    public int Right => Left + Width;

    public int Bottom => Top + Height;

    public bool IsEmpty => Width <= 0 || Height <= 0;

    public static ScreenRect FromEdges(int left, int top, int right, int bottom) =>
        new(left, top, right - left, bottom - top);

    /// <summary>Area shared with <paramref name="other"/>; 0 when disjoint.</summary>
    public long IntersectionArea(ScreenRect other)
    {
        var left = Math.Max(Left, other.Left);
        var top = Math.Max(Top, other.Top);
        var right = Math.Min(Right, other.Right);
        var bottom = Math.Min(Bottom, other.Bottom);

        if (right <= left || bottom <= top)
        {
            return 0;
        }

        return (long)(right - left) * (bottom - top);
    }

    public bool Contains(ScreenRect other) =>
        other.Left >= Left && other.Top >= Top && other.Right <= Right && other.Bottom <= Bottom;
}

/// <summary>
/// One display, as reported by Windows and flattened into plain data so the
/// placement rules can be tested without a desktop.
/// </summary>
/// <param name="DeviceId">
/// Identity used to recognise this display after a restart. See
/// <see cref="MonitorKey"/> for why this is derived from geometry.
/// </param>
/// <param name="WorkArea">Usable area, taskbar and docked bars excluded.</param>
/// <param name="IsPrimary">Whether this is the primary display.</param>
/// <param name="Index">1-based enumeration order, for <c>--monitor 2</c>.</param>
public sealed record MonitorInfo(string DeviceId, ScreenRect WorkArea, bool IsPrimary, int Index);

/// <summary>
/// Builds the persisted identity of a display.
/// </summary>
/// <remarks>
/// WinUI's <c>DisplayArea.DisplayId</c> is a per-session handle value: it is
/// not stable across a reboot, a driver reload, or unplugging and replugging a
/// wall display, which is exactly when window restore has to make a decision.
/// Position and size are stable in practice and, more usefully, they are stable
/// in the way that matters — "the 3840x2160 panel at +1920,0" is the same wall
/// display tomorrow, and genuinely is not the same display if that geometry has
/// gone. Matching on it makes a removed monitor detectable rather than silently
/// restoring a window nobody can see.
/// </remarks>
public static class MonitorKey
{
    /// <summary>Identity key for a display, from its outer bounds.</summary>
    public static string For(ScreenRect outerBounds) => string.Create(
        System.Globalization.CultureInfo.InvariantCulture,
        $"{outerBounds.Left},{outerBounds.Top},{outerBounds.Width}x{outerBounds.Height}");
}
