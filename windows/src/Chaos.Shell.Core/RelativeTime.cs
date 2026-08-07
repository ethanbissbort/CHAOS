using System.Globalization;

namespace Chaos.Shell.Core;

/// <summary>
/// Short, unambiguous age wording for tray tooltips and status lines.
/// </summary>
/// <remarks>
/// Rounds down, never up: an age shown as "5 s" is at least five seconds old.
/// Rounding the other way would let a stale reading present as fresher than it
/// is, which is the one direction this platform is not allowed to err in.
/// </remarks>
public static class RelativeTime
{
    /// <summary>Wording used wherever an age is genuinely unknown.</summary>
    public const string NeverText = "never";

    /// <summary>"3 s", "2 m", "1 h 05 m", "4 d 02 h".</summary>
    public static string Describe(TimeSpan age)
    {
        if (age < TimeSpan.Zero)
        {
            age = TimeSpan.Zero;
        }

        if (age.TotalSeconds < 60)
        {
            return string.Create(CultureInfo.InvariantCulture, $"{(int)age.TotalSeconds} s");
        }

        if (age.TotalMinutes < 60)
        {
            return string.Create(CultureInfo.InvariantCulture, $"{(int)age.TotalMinutes} m");
        }

        if (age.TotalHours < 24)
        {
            return string.Create(
                CultureInfo.InvariantCulture,
                $"{(int)age.TotalHours} h {age.Minutes:00} m");
        }

        return string.Create(
            CultureInfo.InvariantCulture,
            $"{(int)age.TotalDays} d {age.Hours:00} h");
    }

    /// <summary>"3 s ago", or "never" when there is no timestamp at all.</summary>
    public static string DescribeAge(TimeSpan? age) =>
        age is { } value ? Describe(value) + " ago" : NeverText;
}
