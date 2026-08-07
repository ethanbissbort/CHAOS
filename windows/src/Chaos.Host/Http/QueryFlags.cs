using Microsoft.Extensions.Primitives;

namespace Chaos.Host.Http;

/// <summary>
/// Reads boolean query-string flags.
/// </summary>
/// <remarks>
/// <para>
/// Only an explicit affirmative counts. <c>?force</c> with no value counts,
/// because that is what a link and a curl one-liner produce and refusing it
/// would be a usability trap; anything else — absent, empty-valued, <c>0</c>,
/// <c>maybe</c> — is false.
/// </para>
/// <para>
/// The asymmetry is deliberate. These flags gate actions an operator has to
/// have meant, so an unparseable value must never be read as consent.
/// </para>
/// </remarks>
internal static class QueryFlags
{
    private static readonly string[] Affirmatives = ["true", "1", "yes", "on"];

    /// <summary>Whether a query-string value states yes.</summary>
    /// <param name="values">The raw query values for the parameter.</param>
    /// <returns>True only for an explicit affirmative, or a valueless present flag.</returns>
    public static bool IsTrue(StringValues values)
    {
        if (values.Count == 0)
        {
            return false;
        }

        foreach (var value in values)
        {
            // "?force" with no "=" arrives as an empty string: the flag is
            // present, so it is meant.
            if (value is null || value.Length == 0)
            {
                return true;
            }

            if (Array.Exists(Affirmatives, affirmative =>
                    string.Equals(value.Trim(), affirmative, StringComparison.OrdinalIgnoreCase)))
            {
                return true;
            }
        }

        return false;
    }
}
