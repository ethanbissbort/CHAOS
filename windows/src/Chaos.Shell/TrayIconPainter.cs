using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using Chaos.Shell.Core;

namespace Chaos.Shell;

/// <summary>
/// Draws the notification-area icon from the live tray state.
///
/// Colour is never the only channel. Each state also differs in SHAPE and
/// GLYPH, so the icon still reads at 16 px, in a screenshot, and for an
/// operator who cannot distinguish red from amber:
///
///   Starting     hollow ring, no glyph      — nothing known yet
///   Unreachable  hollow ring, "?"           — state UNKNOWN, not "no alarms"
///   Stale        dashed ring, last count    — last known, explicitly aged
///   Normal       filled disc, check         — current answer: nothing active
///   Warning..    filled disc, count         — current answer, worst severity
///
/// The hollow-versus-filled distinction is the important one: a filled calm
/// icon must only ever appear when the platform actually answered.
/// </summary>
internal static class TrayIconPainter
{
    public static Icon Draw(TrayState state)
    {
        const int size = 32;
        using var bitmap = new Bitmap(size, size);
        using (var g = Graphics.FromImage(bitmap))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            g.Clear(Color.Transparent);

            var colour = ColourFor(state.Icon);
            var rect = new Rectangle(2, 2, size - 5, size - 5);

            if (state.DataIsCurrent)
            {
                using var fill = new SolidBrush(colour);
                g.FillEllipse(fill, rect);
            }
            else
            {
                // Not current: hollow, and dashed when it is a stale reading
                // rather than no reading at all.
                using var pen = new Pen(colour, 3f);
                if (state.Icon == TrayIconState.Stale)
                {
                    pen.DashStyle = DashStyle.Dash;
                }
                g.DrawEllipse(pen, rect);
            }

            var glyph = GlyphFor(state);
            if (glyph.Length > 0)
            {
                using var font = new Font("Segoe UI", glyph.Length > 2 ? 11f : 14f, FontStyle.Bold);
                using var text = new SolidBrush(state.DataIsCurrent ? Color.FromArgb(26, 13, 5) : colour);
                var measured = g.MeasureString(glyph, font);
                g.DrawString(glyph, font, text,
                    (size - measured.Width) / 2f,
                    (size - measured.Height) / 2f);
            }
        }

        return Icon.FromHandle(bitmap.GetHicon());
    }

    private static Color ColourFor(TrayIconState state) => state switch
    {
        TrayIconState.Starting => Color.FromArgb(107, 117, 137),
        TrayIconState.Unreachable => Color.FromArgb(107, 117, 137),
        TrayIconState.Stale => Color.FromArgb(245, 193, 68),
        TrayIconState.Normal => Color.FromArgb(46, 194, 126),
        _ => Color.FromArgb(229, 72, 77),
    };

    private static string GlyphFor(TrayState state)
    {
        if (state.Icon == TrayIconState.Unreachable)
        {
            return "?";
        }
        if (state.Icon == TrayIconState.Starting)
        {
            return string.Empty;
        }
        if (!string.IsNullOrEmpty(state.BadgeText))
        {
            return state.BadgeText.Length > 3 ? "9+" : state.BadgeText;
        }
        return state.Icon == TrayIconState.Normal ? "✓" : string.Empty;
    }
}
