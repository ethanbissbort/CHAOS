namespace Chaos.Shell.Core;

/// <summary>Where the help window ended up pointing, and why.</summary>
public enum HelpSourceKind
{
    /// <summary>A file on this machine. Works with the platform stopped.</summary>
    LocalFile = 0,

    /// <summary>The documentation listener on its own port.</summary>
    DocumentationPort = 1,

    /// <summary>The gateway's copy, under the operator console.</summary>
    GatewayServed = 2,

    /// <summary>Nothing was found.</summary>
    Missing = 3,
}

/// <summary>A resolved help location.</summary>
/// <param name="Kind">Which source was chosen.</param>
/// <param name="Target">Where to point the WebView2. Null when nothing was found.</param>
/// <param name="Summary">One line an operator can act on.</param>
/// <param name="WorksOffline">Whether this source survives the platform being stopped.</param>
/// <param name="Searched">Every candidate considered, in order, for the diagnostic.</param>
public sealed record HelpLocation(
    HelpSourceKind Kind,
    Uri? Target,
    string Summary,
    bool WorksOffline,
    IReadOnlyList<string> Searched);

/// <summary>
/// Chooses where the help window reads from.
///
/// The order is deliberate and is the whole reason this is a separate decision
/// rather than a URL constant: <b>a local file beats anything served</b>. Help
/// is most needed when the platform is down, and a help window that goes blank
/// at exactly the moment somebody needs the troubleshooting page is worse than
/// no help button at all. The served copies are fallbacks for an install that
/// has not been given the offline build.
/// </summary>
public static class HelpLocator
{
    /// <summary>The single-file build, relative to the application directory.</summary>
    public static readonly string[] LocalCandidates =
    [
        @"web\docs\chaos-help-offline.html",
        @"app\web\docs\chaos-help-offline.html",
        @"docs\chaos-help-offline.html",
        @"..\app\web\docs\chaos-help-offline.html",
    ];

    /// <summary>
    /// Resolves the help source.
    /// </summary>
    /// <param name="applicationDirectory">Directory the shell is running from.</param>
    /// <param name="fileExists">Existence probe, injected so this is testable.</param>
    /// <param name="documentationUrl">
    /// The documentation listener's URL as reported by <c>/host/info</c>, or null
    /// when the gateway did not report one (older host, or docs disabled).
    /// </param>
    /// <param name="endpoints">The gateway, for the last-resort served copy.</param>
    /// <param name="gatewayReachable">
    /// Whether the gateway answered recently. A served fallback is only offered
    /// when it is: pointing a help window at a URL known to be dead would
    /// produce the blank window this type exists to prevent.
    /// </param>
    public static HelpLocation Resolve(
        string applicationDirectory,
        Func<string, bool> fileExists,
        Uri? documentationUrl,
        HostEndpoints endpoints,
        bool gatewayReachable)
    {
        ArgumentNullException.ThrowIfNull(fileExists);
        ArgumentNullException.ThrowIfNull(endpoints);

        var searched = new List<string>();

        foreach (var candidate in LocalCandidates)
        {
            var full = Path.GetFullPath(Path.Combine(applicationDirectory ?? ".", candidate));
            searched.Add(full);
            if (fileExists(full))
            {
                return new HelpLocation(
                    HelpSourceKind.LocalFile,
                    new Uri(full),
                    "Offline help, read from this machine. It stays readable if the platform stops.",
                    WorksOffline: true,
                    searched);
            }
        }

        if (documentationUrl is not null)
        {
            searched.Add(documentationUrl.ToString());
            if (gatewayReachable)
            {
                return new HelpLocation(
                    HelpSourceKind.DocumentationPort,
                    documentationUrl,
                    "Served by the platform on its documentation port. It will not open if the platform stops.",
                    WorksOffline: false,
                    searched);
            }
        }

        var served = new Uri(endpoints.BaseUri, "ui/docs/");
        searched.Add(served.ToString());
        if (gatewayReachable)
        {
            return new HelpLocation(
                HelpSourceKind.GatewayServed,
                served,
                "Served by the gateway alongside the console. It will not open if the platform stops.",
                WorksOffline: false,
                searched);
        }

        return new HelpLocation(
            HelpSourceKind.Missing,
            null,
            "No help was found on this machine, and the platform is not answering. "
                + "Build the documentation from Visual Studio (build the Chaos.Runtime project) "
                + "so a copy is installed alongside the application.",
            WorksOffline: false,
            searched);
    }
}
