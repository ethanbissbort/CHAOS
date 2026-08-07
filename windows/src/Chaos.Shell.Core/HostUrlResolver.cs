namespace Chaos.Shell.Core;

/// <summary>Where the gateway address the shell is using actually came from.</summary>
public enum HostUrlSource
{
    /// <summary>Nothing was configured; the built-in loopback default is in use.</summary>
    Default = 0,

    /// <summary>The <c>CHAOS_HOST_URL</c> environment variable.</summary>
    Environment = 1,

    /// <summary>A <c>--host</c> command-line argument.</summary>
    CommandLine = 2,

    /// <summary>The address the operator set on the Settings page.</summary>
    Settings = 3,
}

/// <summary>Outcome of resolving the gateway address.</summary>
/// <param name="Endpoints">Endpoints to use. Never null — falls back to the default.</param>
/// <param name="Source">Which input won.</param>
/// <param name="Problem">
/// Non-null when a supplied value was rejected. The shell shows this rather
/// than silently pretending the operator's setting was honoured.
/// </param>
public sealed record HostResolution(HostEndpoints Endpoints, HostUrlSource Source, string? Problem)
{
    public bool UsedFallback => Problem is not null;
}

/// <summary>
/// Resolves the gateway base address from, in order of precedence, the command
/// line, the environment, then the loopback default.
/// </summary>
public static class HostUrlResolver
{
    public const string EnvironmentVariable = "CHAOS_HOST_URL";

    public static HostResolution Resolve(string? commandLineValue, string? environmentValue)
    {
        if (!string.IsNullOrWhiteSpace(commandLineValue))
        {
            return Build(commandLineValue!, HostUrlSource.CommandLine, "--host");
        }

        if (!string.IsNullOrWhiteSpace(environmentValue))
        {
            return Build(environmentValue!, HostUrlSource.Environment, EnvironmentVariable);
        }

        return new HostResolution(HostEndpoints.Default, HostUrlSource.Default, null);
    }

    /// <summary>
    /// Resolves with the operator's saved settings behind the per-launch
    /// overrides.
    /// </summary>
    /// <remarks>
    /// Precedence, and why: <c>--host</c> then <c>CHAOS_HOST_URL</c> then the
    /// Settings page then the built-in default. The command line and the
    /// environment are things somebody did to THIS launch — a shortcut for a
    /// second node, a one-off check — so they win over the standing preference
    /// without overwriting it. The launcher says which one is in force, so an
    /// operator whose Settings address appears to be ignored can see why.
    /// </remarks>
    public static HostResolution Resolve(
        string? commandLineValue,
        string? environmentValue,
        ShellSettings? settings)
    {
        if (!string.IsNullOrWhiteSpace(commandLineValue))
        {
            return Build(commandLineValue!, HostUrlSource.CommandLine, "--host");
        }

        if (!string.IsNullOrWhiteSpace(environmentValue))
        {
            return Build(environmentValue!, HostUrlSource.Environment, EnvironmentVariable);
        }

        if (settings is not null)
        {
            var uri = settings.TryComposeGatewayUri();
            if (uri is not null)
            {
                return new HostResolution(HostEndpoints.For(uri), HostUrlSource.Settings, null);
            }

            return new HostResolution(
                HostEndpoints.Default,
                HostUrlSource.Default,
                $"The gateway address saved in Settings ('{settings.HostAddress}' port "
                + $"{settings.HostPort}) is not usable, so the shell is using {HostEndpoints.Default}. "
                + "Open Settings to correct it.");
        }

        return new HostResolution(HostEndpoints.Default, HostUrlSource.Default, null);
    }

    /// <summary>
    /// One line naming where the address in force came from, for the launcher.
    /// </summary>
    public static string DescribeSource(HostResolution resolution)
    {
        ArgumentNullException.ThrowIfNull(resolution);

        return resolution.Source switch
        {
            HostUrlSource.CommandLine =>
                $"Using {resolution.Endpoints.BaseUri} from the --host argument this shell was "
                + "started with, which overrides the address in Settings for this launch only.",

            HostUrlSource.Environment =>
                $"Using {resolution.Endpoints.BaseUri} from the {EnvironmentVariable} environment "
                + "variable, which overrides the address in Settings for this launch only.",

            HostUrlSource.Settings =>
                $"Using {resolution.Endpoints.BaseUri} from Settings.",

            _ => $"Using the built-in default {resolution.Endpoints.BaseUri}; no address has been set.",
        };
    }

    /// <summary>
    /// Parses a user-supplied address. Accepts a full URL, <c>host:port</c> or a
    /// bare host; anything else is rejected with a message naming the input.
    /// </summary>
    public static bool TryParse(string value, out Uri? baseUri, out string? problem)
    {
        baseUri = null;
        problem = null;

        var text = value?.Trim() ?? string.Empty;
        if (text.Length == 0)
        {
            problem = "the address is empty";
            return false;
        }

        // Tolerate "chaos-node:8080" and "chaos-node", which is what an operator
        // types. Anything with a scheme we do not serve is rejected outright
        // rather than coerced into something that might silently work.
        var hasScheme = text.Contains("://", StringComparison.Ordinal);
        if (!hasScheme)
        {
            text = "http://" + text;
        }

        if (!Uri.TryCreate(text, UriKind.Absolute, out var parsed))
        {
            problem = $"'{value}' is not a valid address";
            return false;
        }

        if (parsed.Scheme != Uri.UriSchemeHttp && parsed.Scheme != Uri.UriSchemeHttps)
        {
            problem = $"'{value}' uses scheme '{parsed.Scheme}'; the gateway speaks http or https";
            return false;
        }

        if (string.IsNullOrEmpty(parsed.Host))
        {
            problem = $"'{value}' has no host";
            return false;
        }

        // "chaos-node" on its own means the gateway, which is not on port 80.
        // A written-out scheme or an explicit port is always honoured as typed.
        if (!hasScheme && !HasExplicitPort(text))
        {
            parsed = new UriBuilder(parsed) { Port = HostEndpoints.DefaultGatewayPort }.Uri;
        }

        baseUri = parsed;
        return true;
    }

    /// <summary>
    /// True when the authority carries a <c>:port</c>. IPv6 literals are
    /// bracketed, so their internal colons do not count.
    /// </summary>
    private static bool HasExplicitPort(string absoluteUrl)
    {
        var afterScheme = absoluteUrl.AsSpan(absoluteUrl.IndexOf("://", StringComparison.Ordinal) + 3);
        var slash = afterScheme.IndexOf('/');
        var authority = slash >= 0 ? afterScheme[..slash] : afterScheme;

        var colon = authority.LastIndexOf(':');
        if (colon < 0 || colon < authority.LastIndexOf(']'))
        {
            return false;
        }

        var port = authority[(colon + 1)..];
        if (port.IsEmpty)
        {
            return false;
        }

        foreach (var c in port)
        {
            if (!char.IsAsciiDigit(c))
            {
                return false;
            }
        }

        return true;
    }

    private static HostResolution Build(string value, HostUrlSource source, string origin)
    {
        if (TryParse(value, out var uri, out var problem) && uri is not null)
        {
            return new HostResolution(HostEndpoints.For(uri), source, null);
        }

        return new HostResolution(
            HostEndpoints.Default,
            HostUrlSource.Default,
            $"{origin} was ignored because {problem}. Using {HostEndpoints.Default}.");
    }
}
