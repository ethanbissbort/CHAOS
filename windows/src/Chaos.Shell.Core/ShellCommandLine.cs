namespace Chaos.Shell.Core;

/// <summary>Everything a launch asked for, after parsing the command line.</summary>
/// <param name="Activation">
/// What this launch wants shown. Also the exact payload a second instance hands
/// to the first, so a shortcut behaves identically whether or not the shell was
/// already running.
/// </param>
/// <param name="HostOverride">Raw <c>--host</c> value, unvalidated.</param>
/// <param name="Problems">
/// Arguments that were not understood. Never fatal — the shell starts anyway and
/// surfaces these, because refusing to launch a control-room UI over a typo in a
/// shortcut is worse than launching with defaults and saying so.
/// </param>
public sealed record ShellStartupOptions(
    ActivationPayload Activation,
    string? HostOverride,
    IReadOnlyList<string> Problems)
{
    public static readonly ShellStartupOptions Default =
        new(ActivationPayload.FocusConsole, null, Array.Empty<string>());
}

/// <summary>
/// Parses the shell's command line.
/// </summary>
/// <remarks>
/// Supported:
/// <list type="bullet">
///   <item><c>--host &lt;url&gt;</c> gateway address (also <c>CHAOS_HOST_URL</c>)</item>
///   <item><c>--annunciator</c> open the annunciator panel</item>
///   <item><c>--acknowledge</c> open the annunciator for acknowledgement</item>
///   <item><c>--monitor &lt;n|name|primary&gt;</c> which display to place it on</item>
///   <item><c>--fullscreen</c> wall-display mode</item>
///   <item><c>--always-on-top</c> keep the panel above other windows</item>
/// </list>
/// </remarks>
public static class ShellCommandLine
{
    public static ShellStartupOptions Parse(IReadOnlyList<string>? args)
    {
        if (args is null || args.Count == 0)
        {
            return ShellStartupOptions.Default;
        }

        var problems = new List<string>();
        var target = ActivationTarget.Console;
        string? host = null;
        string? monitor = null;
        var fullScreen = false;
        var alwaysOnTop = false;

        for (var i = 0; i < args.Count; i++)
        {
            var arg = args[i]?.Trim() ?? string.Empty;
            if (arg.Length == 0)
            {
                continue;
            }

            // "--host=x" carries its value inline; switch on the name only.
            var normalized = Normalize(arg);
            var equals = normalized.IndexOf('=');
            var name = equals >= 0 ? normalized[..equals] : normalized;

            switch (name)
            {
                case "host":
                    host = TakeValue(args, ref i, "--host", problems);
                    break;

                case "annunciator":
                    target = ActivationTarget.Annunciator;
                    break;

                case "acknowledge":
                case "ack":
                    target = ActivationTarget.Acknowledge;
                    break;

                case "monitor":
                case "display":
                    monitor = TakeValue(args, ref i, "--monitor", problems);
                    break;

                case "fullscreen":
                case "full-screen":
                case "kiosk":
                    fullScreen = true;
                    break;

                case "always-on-top":
                case "topmost":
                    alwaysOnTop = true;
                    break;

                default:
                    problems.Add($"Unrecognised argument '{arg}' was ignored.");
                    break;
            }
        }

        // --monitor / --fullscreen only mean anything for the annunciator, which
        // is the window that goes on a wall. Say so rather than silently
        // dropping them.
        if (target == ActivationTarget.Console && (monitor is not null || fullScreen))
        {
            problems.Add(
                "--monitor and --fullscreen apply to the annunciator panel; "
                + "add --annunciator to place it on a display.");
        }

        var activation = new ActivationPayload
        {
            Target = target,
            Monitor = monitor,
            FullScreen = fullScreen,
            AlwaysOnTop = alwaysOnTop,
        };

        return new ShellStartupOptions(activation, host, problems);
    }

    /// <summary>Strips a leading <c>--</c>, <c>-</c> or <c>/</c> and lowercases.</summary>
    private static string Normalize(string arg)
    {
        var span = arg.AsSpan();
        if (span.StartsWith("--"))
        {
            span = span[2..];
        }
        else if (span.Length > 0 && (span[0] == '-' || span[0] == '/'))
        {
            span = span[1..];
        }

        return span.ToString().ToLowerInvariant();
    }

    private static string? TakeValue(
        IReadOnlyList<string> args,
        ref int index,
        string name,
        List<string> problems)
    {
        // Accept both "--host x" and "--host=x".
        var current = args[index];
        var equals = current.IndexOf('=');
        if (equals >= 0 && equals + 1 < current.Length)
        {
            return current[(equals + 1)..].Trim();
        }

        if (index + 1 < args.Count && !IsSwitch(args[index + 1]))
        {
            index++;
            return args[index].Trim();
        }

        problems.Add($"{name} needs a value; it was ignored.");
        return null;
    }

    private static bool IsSwitch(string? arg) =>
        !string.IsNullOrEmpty(arg) && (arg[0] == '-' || arg[0] == '/');
}
