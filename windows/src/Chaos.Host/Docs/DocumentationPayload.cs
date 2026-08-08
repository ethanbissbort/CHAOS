using Microsoft.AspNetCore.Http;

namespace Chaos.Host.Docs;

/// <summary>
/// The <c>documentation</c> object on <c>GET /host/info</c>.
/// </summary>
/// <remarks>
/// The shell and the operator console link to the manual from here, so the URL
/// has to be one a browser on the other side of the LAN can actually open. A
/// listener bound to <c>0.0.0.0</c> answers on every interface but
/// <c>http://0.0.0.0:8090/</c> is not a usable link, so the wildcard is replaced
/// with the host the caller used to reach the gateway: ask through
/// <c>http://192.168.1.10:8080/host/info</c> and the answer names
/// <c>http://192.168.1.10:8090/</c>.
/// </remarks>
public static class DocumentationPayload
{
    private static readonly string[] WildcardHosts = ["0.0.0.0", "::", "[::]", "*", "+"];

    /// <summary>Builds the payload.</summary>
    /// <param name="state">The documentation listener's state.</param>
    /// <param name="requestHost">The host the caller used to reach the gateway.</param>
    /// <returns>An object for JSON serialisation.</returns>
    public static object Create(DocumentationSiteState state, HostString requestHost)
    {
        ArgumentNullException.ThrowIfNull(state);

        var snapshot = state.Current;
        var root = state.Root;

        return new
        {
            enabled = state.Options.Enabled,
            status = Describe(snapshot.Status),
            url = BrowsableUrl(state, requestHost),
            listenUrl = state.Options.ListenUrl,
            listeningUrl = snapshot.ListeningUrl,
            generated = root.HasIndex,
            path = root.Path,
            source = root.Source,
            searched = root.SearchedPaths,
            detail = snapshot.Detail,
            error = snapshot.Error,
            servedHere = false,
            note = "The documentation is served on its own port by a listener with no API, no proxy and no write "
                 + "endpoints, so it keeps answering when the platform behind this gateway does not. It is not "
                 + "reachable as a path on the gateway.",
        };
    }

    /// <summary>
    /// The documentation URL a browser on the far side of the LAN can open, or
    /// null when there is nothing to link to.
    /// </summary>
    /// <param name="state">The documentation listener's state.</param>
    /// <param name="requestHost">The host the caller used to reach the gateway.</param>
    /// <returns>An absolute URL ending in <c>/</c>, or null.</returns>
    public static string? BrowsableUrl(DocumentationSiteState state, HostString requestHost)
    {
        ArgumentNullException.ThrowIfNull(state);

        if (!state.Options.Enabled)
        {
            return null;
        }

        // The bound address when it is known: with a configured port of 0 it is
        // the only place the real port appears.
        var candidate = state.Current.ListeningUrl ?? state.Options.ListenUrl;
        var first = candidate.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .FirstOrDefault();

        if (first is null || !Uri.TryCreate(first, UriKind.Absolute, out var uri))
        {
            return null;
        }

        var host = uri.Host;
        if (WildcardHosts.Contains(host, StringComparer.OrdinalIgnoreCase))
        {
            host = requestHost.HasValue && !string.IsNullOrWhiteSpace(requestHost.Host)
                ? requestHost.Host
                : "localhost";
        }

        // An IPv6 literal has to go back into the URL in brackets.
        if (host.Contains(':', StringComparison.Ordinal) && !host.StartsWith('['))
        {
            host = $"[{host}]";
        }

        return $"{uri.Scheme}://{host}:{uri.Port}/";
    }

    private static string Describe(DocumentationSiteStatus status) => status switch
    {
        DocumentationSiteStatus.Listening => "listening",
        DocumentationSiteStatus.Disabled => "not-running",
        DocumentationSiteStatus.Failed => "failed",
        _ => "not-started",
    };
}
