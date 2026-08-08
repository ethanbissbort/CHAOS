using System.Text.Json.Serialization;

namespace Chaos.Shell.Core;

/// <summary>Which colour scheme the shell chrome uses.</summary>
public enum ShellTheme
{
    /// <summary>Follow the Windows app theme.</summary>
    System = 0,

    /// <summary>Always dark, whatever Windows says.</summary>
    Dark = 1,

    /// <summary>Always light.</summary>
    Light = 2,
}

/// <summary>
/// Every setting the shell exposes natively, so that an operator never has to
/// open a config file, a terminal or regedit to make the product work.
/// </summary>
/// <remarks>
/// <para>
/// Persisted as JSON next to the window layout. It is deliberately a separate
/// file from <see cref="ShellLayout"/>: layout is disposable cosmetic state that
/// is rewritten on every window move, settings are deliberate operator choices
/// that must not be lost because a layout write raced with a crash.
/// </para>
/// <para>
/// The address is stored decomposed — scheme, host, port — rather than as one
/// URL string, because that is the shape the settings page presents and because
/// validating three small fields gives an operator a message naming the field
/// they got wrong instead of "that URL is invalid".
/// </para>
/// </remarks>
public sealed record ShellSettings
{
    /// <summary>Bump only for an incompatible change to the fields below.</summary>
    public const int CurrentVersion = 1;

    /// <summary>Everything at its built-in default.</summary>
    public static readonly ShellSettings Defaults = new();

    [JsonPropertyName("v")]
    public int Version { get; init; } = CurrentVersion;

    // -- Where the gateway is ------------------------------------------------

    /// <summary>Host name or IP of the gateway. No scheme, no port.</summary>
    [JsonPropertyName("hostAddress")]
    public string HostAddress { get; init; } = HostEndpoints.DefaultGatewayHost;

    /// <summary>TCP port the gateway listens on.</summary>
    [JsonPropertyName("hostPort")]
    public int HostPort { get; init; } = HostEndpoints.DefaultGatewayPort;

    /// <summary>Whether to reach the gateway over TLS.</summary>
    [JsonPropertyName("useHttps")]
    public bool UseHttps { get; init; }

    // -- Lifecycle -----------------------------------------------------------

    /// <summary>
    /// Start the platform automatically when the shell launches, if it is not
    /// already up. On by default: the whole point of the launcher is that
    /// double-clicking the executable is the only step.
    /// </summary>
    [JsonPropertyName("autoStartPlatform")]
    public bool AutoStartPlatform { get; init; } = true;

    /// <summary>Register the shell to start when this user logs in.</summary>
    [JsonPropertyName("startWithWindows")]
    public bool StartWithWindows { get; init; }

    /// <summary>
    /// Prefer the Windows service when both a service and an executable are
    /// available. On by default, because a service keeps the homestead
    /// controlled when nobody is logged in.
    /// </summary>
    [JsonPropertyName("preferWindowsService")]
    public bool PreferWindowsService { get; init; } = true;

    /// <summary>Name of the Windows service that supervises the platform.</summary>
    [JsonPropertyName("serviceName")]
    public string ServiceName { get; init; } = ShellMessages.ServiceName;

    /// <summary>
    /// Explicit path to <c>Chaos.Host.exe</c>. Null means "look for it" — see
    /// <see cref="HostExecutableLocator"/>.
    /// </summary>
    [JsonPropertyName("hostExecutablePath")]
    public string? HostExecutablePath { get; init; }

    // -- Storage -------------------------------------------------------------

    /// <summary>
    /// Where the platform database lives. Either a file path or a full
    /// connection URL. Null means "leave the platform's own default alone".
    /// </summary>
    /// <remarks>
    /// Only applied to a platform this shell starts itself. A Windows service
    /// reads its own configuration and this shell does not rewrite it — see
    /// <see cref="SettingsChangeImpact"/>, which says so to the operator.
    /// </remarks>
    [JsonPropertyName("databasePath")]
    public string? DatabasePath { get; init; }

    /// <summary>Data directory handed to a platform this shell starts. Null leaves it alone.</summary>
    [JsonPropertyName("dataDirectory")]
    public string? DataDirectory { get; init; }

    // -- Presentation --------------------------------------------------------

    [JsonPropertyName("theme")]
    [JsonConverter(typeof(JsonStringEnumConverter<ShellTheme>))]
    public ShellTheme Theme { get; init; } = ShellTheme.System;

    /// <summary>
    /// Which display the annunciator opens on: a 0-based index, a device name,
    /// "primary", or null for the primary display.
    /// </summary>
    [JsonPropertyName("annunciatorMonitor")]
    public string? AnnunciatorMonitor { get; init; }

    /// <summary>Open the annunciator full-screen, for a wall panel.</summary>
    [JsonPropertyName("annunciatorFullScreen")]
    public bool AnnunciatorFullScreen { get; init; }

    /// <summary>
    /// The gateway address these settings describe, or null when the fields do
    /// not compose into a valid absolute URI. Callers that need a guaranteed
    /// address use <see cref="ShellSettingsValidator"/> first.
    /// </summary>
    public Uri? TryComposeGatewayUri()
    {
        var address = HostAddress?.Trim();
        if (string.IsNullOrEmpty(address) || HostPort is < 1 or > 65535)
        {
            return null;
        }

        // An IPv6 literal has to be bracketed in an authority. Accepting the
        // bare form is worth it: an operator typing an address from a router
        // page will not add the brackets.
        if (address.Contains(':', StringComparison.Ordinal) && !address.StartsWith('['))
        {
            address = "[" + address + "]";
        }

        var scheme = UseHttps ? Uri.UriSchemeHttps : Uri.UriSchemeHttp;
        return Uri.TryCreate($"{scheme}://{address}:{HostPort}/", UriKind.Absolute, out var uri)
            ? uri
            : null;
    }

    /// <summary>
    /// Endpoints for these settings, falling back to the built-in default when
    /// the address does not compose. Never returns null: a shell that refuses
    /// to open because a preference is malformed is worse than one that opens
    /// against the default and says so.
    /// </summary>
    public HostEndpoints Endpoints()
    {
        var uri = TryComposeGatewayUri();
        return uri is null ? HostEndpoints.Default : HostEndpoints.For(uri);
    }

    /// <summary>
    /// Settings describing <paramref name="uri"/>, keeping every other field.
    /// Used when a <c>--host</c> argument or the environment overrides the
    /// stored address for one launch.
    /// </summary>
    public ShellSettings WithGateway(Uri uri)
    {
        ArgumentNullException.ThrowIfNull(uri);

        var host = uri.Host;

        // Uri.Host brackets IPv6 literals; the settings field stores the bare
        // form so the settings box shows what the operator typed.
        if (host.StartsWith('[') && host.EndsWith(']'))
        {
            host = host[1..^1];
        }

        return this with
        {
            HostAddress = host,
            HostPort = uri.Port,
            UseHttps = string.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase),
        };
    }
}
