namespace Chaos.Shell.Core;

/// <summary>
/// Every URL the shell needs, derived from one gateway base address.
/// </summary>
/// <remarks>
/// The shell never carries its own copy of the web assets. It points WebView2
/// at the gateway's copy so the desktop app and the browsers on the LAN are
/// looking at the same files. Paths here mirror how the platform mounts them
/// (<c>src/chaos/api/app.py</c>): the console at <c>/</c>, the static
/// bundle under <c>/ui/</c>, the API under <c>/api/v1</c>.
/// </remarks>
public sealed record HostEndpoints
{
    public const int DefaultGatewayPort = 8080;
    public const string DefaultGatewayHost = "127.0.0.1";

    private HostEndpoints(Uri baseUri) => BaseUri = baseUri;

    /// <summary>Gateway root, always with a trailing slash.</summary>
    public Uri BaseUri { get; }

    /// <summary>Operator console — what the main window loads.</summary>
    public Uri Console => BaseUri;

    /// <summary>Alarm annunciator panel — the second window / wall display.</summary>
    public Uri Annunciator => new(BaseUri, "ui/annunciator.html");

    /// <summary>Platform health, used as the startup readiness probe.</summary>
    public Uri Health => new(BaseUri, "health");

    /// <summary>Active alarms, polled for tray state.</summary>
    public Uri ActiveAlarms => new(BaseUri, "api/v1/alarms/active");

    /// <summary>Gateway identity and configuration, for the launcher's diagnostic.</summary>
    public Uri HostInfo => new(BaseUri, "host/info");

    /// <summary>
    /// First-run setup state. A gateway older than this shell answers 404 here,
    /// which the launcher reports as "this platform is too old to say" rather
    /// than as a failure.
    /// </summary>
    public Uri Setup => new(BaseUri, "host/setup");

    /// <summary>Triggers or retries first-run setup. POST.</summary>
    public Uri RunSetup => new(BaseUri, "host/setup/run");

    /// <summary>
    /// The setup-run URL, optionally forced.
    /// </summary>
    /// <remarks>
    /// The gateway refuses an ordinary run when automatic setup is off or when
    /// it assessed the database as needing a human decision, and only
    /// <c>?force=true</c> gets past that. Forcing is never inferred by the
    /// shell: it is passed only when the setup report itself said it was
    /// required, because that guard exists to stop a run nobody chose.
    /// </remarks>
    public Uri RunSetupUrl(bool force) =>
        force ? new(BaseUri, "host/setup/run?force=true") : RunSetup;

    /// <summary>OpenAPI docs, offered from the Help menu.</summary>
    public Uri ApiDocs => new(BaseUri, "docs");

    public static HostEndpoints Default => new(
        new Uri($"http://{DefaultGatewayHost}:{DefaultGatewayPort}/"));

    /// <summary>Builds endpoints from an already-validated absolute URI.</summary>
    public static HostEndpoints For(Uri baseUri)
    {
        ArgumentNullException.ThrowIfNull(baseUri);

        if (!baseUri.IsAbsoluteUri)
        {
            throw new ArgumentException("The gateway base address must be absolute.", nameof(baseUri));
        }

        return new HostEndpoints(WithTrailingSlash(baseUri));
    }

    /// <summary>
    /// The address to hand to someone on the LAN. The gateway keeps serving the
    /// browser UI whatever this desktop shell is doing, so this URL stays valid
    /// even with the shell closed.
    /// </summary>
    public Uri LanUrlFor(string hostNameOrAddress)
    {
        if (string.IsNullOrWhiteSpace(hostNameOrAddress))
        {
            return BaseUri;
        }

        var builder = new UriBuilder(BaseUri)
        {
            Host = hostNameOrAddress.Trim(),
        };
        return WithTrailingSlash(builder.Uri);
    }

    /// <summary>
    /// True when the base address is a loopback literal — the case where the
    /// URL is useless to anyone else and the UI should say so rather than
    /// inviting an operator to share it.
    /// </summary>
    public bool IsLoopback => BaseUri.IsLoopback;

    private static Uri WithTrailingSlash(Uri uri)
    {
        if (uri.AbsolutePath.EndsWith('/'))
        {
            return uri;
        }

        return new UriBuilder(uri) { Path = uri.AbsolutePath + "/" }.Uri;
    }

    public override string ToString() => BaseUri.ToString();
}
