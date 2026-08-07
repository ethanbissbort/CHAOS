using System.Text.Json.Serialization;

namespace Chaos.Shell.Core;

/// <summary>A remembered window position.</summary>
public sealed record WindowPlacement
{
    [JsonPropertyName("left")]
    public int Left { get; init; }

    [JsonPropertyName("top")]
    public int Top { get; init; }

    [JsonPropertyName("width")]
    public int Width { get; init; }

    [JsonPropertyName("height")]
    public int Height { get; init; }

    [JsonPropertyName("maximized")]
    public bool Maximized { get; init; }

    /// <summary>
    /// The display this window was last on. Checked on restore: if the display
    /// is gone, the saved coordinates are meaningless and are not reused.
    /// </summary>
    [JsonPropertyName("monitor")]
    public string? MonitorDeviceId { get; init; }

    [JsonPropertyName("fullScreen")]
    public bool FullScreen { get; init; }

    [JsonPropertyName("alwaysOnTop")]
    public bool AlwaysOnTop { get; init; }

    /// <summary>WebView2 zoom factor, 0.25–5.0.</summary>
    [JsonPropertyName("zoom")]
    public double Zoom { get; init; } = 1.0;

    [JsonIgnore]
    public ScreenRect Bounds => new(Left, Top, Width, Height);

    public static WindowPlacement FromBounds(
        ScreenRect bounds,
        string? monitorDeviceId = null,
        bool maximized = false) => new()
        {
            Left = bounds.Left,
            Top = bounds.Top,
            Width = bounds.Width,
            Height = bounds.Height,
            MonitorDeviceId = monitorDeviceId,
            Maximized = maximized,
        };
}

/// <summary>Why a restore did not land where it was asked to.</summary>
public enum PlacementAdjustment
{
    /// <summary>Restored exactly as saved.</summary>
    None = 0,

    /// <summary>Nothing was saved, or the saved record was unusable.</summary>
    Defaulted = 1,

    /// <summary>The saved display is no longer attached.</summary>
    MonitorMissing = 2,

    /// <summary>The saved rectangle no longer fits the display it named.</summary>
    ClampedToWorkArea = 3,

    /// <summary>The saved size was smaller than the window can usefully be.</summary>
    ResizedToMinimum = 4,
}

/// <summary>Result of resolving a saved placement against the current desktop.</summary>
/// <param name="Bounds">Where to put the window now.</param>
/// <param name="Monitor">The display it will land on.</param>
/// <param name="Maximized">Whether to restore maximized.</param>
/// <param name="Adjustment">Why it differs from what was saved, if it does.</param>
public sealed record PlacementResolution(
    ScreenRect Bounds,
    MonitorInfo Monitor,
    bool Maximized,
    PlacementAdjustment Adjustment)
{
    public bool WasAdjusted => Adjustment != PlacementAdjustment.None;
}
