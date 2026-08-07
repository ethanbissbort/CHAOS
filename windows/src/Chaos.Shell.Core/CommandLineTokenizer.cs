using System.Text;

namespace Chaos.Shell.Core;

/// <summary>
/// Splits a raw Windows command line into arguments.
/// </summary>
/// <remarks>
/// Single-instance activation delivers the second launch's command line to the
/// running instance as one string, not as argv, so it has to be re-split before
/// <see cref="ShellCommandLine"/> can read it. The rules implemented here are
/// the <c>CommandLineToArgvW</c> ones: a run of backslashes is literal unless
/// it precedes a quote, in which case each pair collapses to one backslash and
/// an odd trailing backslash escapes the quote. Paths like
/// <c>"C:\Program Files\CHAOS\"</c> go wrong under any simpler rule.
/// </remarks>
public static class CommandLineTokenizer
{
    public static IReadOnlyList<string> Split(string? commandLine)
    {
        var result = new List<string>();

        if (string.IsNullOrWhiteSpace(commandLine))
        {
            return result;
        }

        var current = new StringBuilder();
        var inQuotes = false;
        var started = false;
        var backslashes = 0;

        foreach (var c in commandLine!)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }

            if (c == '"')
            {
                // Pairs of backslashes are literal; an odd one escapes the quote.
                current.Append('\\', backslashes / 2);
                if (backslashes % 2 == 1)
                {
                    current.Append('"');
                }
                else
                {
                    inQuotes = !inQuotes;
                }

                backslashes = 0;
                started = true;
                continue;
            }

            current.Append('\\', backslashes);
            backslashes = 0;

            if (!inQuotes && (c == ' ' || c == '\t'))
            {
                if (started)
                {
                    result.Add(current.ToString());
                    current.Clear();
                    started = false;
                }

                continue;
            }

            current.Append(c);
            started = true;
        }

        current.Append('\\', backslashes);

        if (started || current.Length > 0)
        {
            result.Add(current.ToString());
        }

        return result;
    }

    /// <summary>
    /// Splits an activation command line and drops the leading executable path
    /// if one is present.
    /// </summary>
    /// <remarks>
    /// Whether the delivered string starts with the executable depends on how
    /// the process was activated, so this decides by inspecting the first token
    /// rather than by assuming either shape. A leading token that is not a
    /// switch and looks like a path to this shell is dropped; anything else is
    /// kept, so <c>--annunciator</c> is never mistaken for a program name.
    /// </remarks>
    public static IReadOnlyList<string> SplitActivationArguments(string? commandLine)
    {
        var tokens = Split(commandLine);
        if (tokens.Count == 0)
        {
            return tokens;
        }

        var first = tokens[0];
        var looksLikeExecutable =
            !first.StartsWith('-')
            && !first.StartsWith('/')
            && (first.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)
                || first.Contains('\\', StringComparison.Ordinal)
                || first.Contains(':', StringComparison.Ordinal));

        return looksLikeExecutable ? tokens.Skip(1).ToList() : tokens;
    }
}
