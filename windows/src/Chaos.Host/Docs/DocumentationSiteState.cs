using Chaos.Host.Configuration;

namespace Chaos.Host.Docs;

/// <summary>
/// What the documentation listener is doing, for <c>/host/info</c> to report.
/// </summary>
/// <remarks>
/// Written once by <see cref="DocumentationSiteHost"/> as the listener starts or
/// declines to start, read on every <c>/host/info</c> request. A single volatile
/// reference to an immutable snapshot, so readers never see a half-updated state
/// and never block — the same shape as <see cref="Health.BackendHealthState"/>.
/// </remarks>
public sealed class DocumentationSiteState
{
    private DocumentationSiteSnapshot _current;

    /// <summary>Creates the state for a configured documentation listener.</summary>
    /// <param name="options">The documentation options, as configured.</param>
    /// <param name="root">Where the generated site was resolved to, or was not.</param>
    public DocumentationSiteState(DocumentationOptions options, DocumentationRootResolution root)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(root);

        Options = options;
        Root = root;
        _current = new DocumentationSiteSnapshot(
            options.Enabled ? DocumentationSiteStatus.NotStarted : DocumentationSiteStatus.Disabled,
            ListeningUrl: null,
            Detail: options.Enabled
                ? "The documentation listener has not started yet."
                : "The documentation listener is switched off by configuration (Chaos:Docs:Enabled=false).",
            Error: null);
    }

    /// <summary>The documentation options this listener was configured with.</summary>
    public DocumentationOptions Options { get; }

    /// <summary>Where the generated site was resolved to, including every path probed.</summary>
    public DocumentationRootResolution Root { get; }

    /// <summary>The current reading.</summary>
    public DocumentationSiteSnapshot Current => Volatile.Read(ref _current);

    /// <summary>Records that the listener is up and serving.</summary>
    /// <param name="listeningUrl">The address Kestrel actually bound.</param>
    public void MarkListening(string listeningUrl) => Volatile.Write(ref _current, new DocumentationSiteSnapshot(
        DocumentationSiteStatus.Listening,
        listeningUrl,
        Root.HasIndex
            ? "Serving the generated documentation site."
            : "Listening, but no site has been generated yet; every page explains how to build it.",
        Error: null));

    /// <summary>Records that the listener was deliberately not started.</summary>
    /// <param name="detail">Why, in a sentence an operator can act on.</param>
    public void MarkNotStarted(string detail) => Volatile.Write(ref _current, new DocumentationSiteSnapshot(
        DocumentationSiteStatus.Disabled,
        ListeningUrl: null,
        detail,
        Error: null));

    /// <summary>Records that the listener could not start.</summary>
    /// <param name="error">The failure, verbatim.</param>
    /// <remarks>
    /// A documentation listener that cannot bind must never stop the gateway: the
    /// control plane matters more than the manual. It is recorded here instead so
    /// <c>/host/info</c> can say so rather than reporting a URL that refuses
    /// connections.
    /// </remarks>
    public void MarkFailed(string error) => Volatile.Write(ref _current, new DocumentationSiteSnapshot(
        DocumentationSiteStatus.Failed,
        ListeningUrl: null,
        "The documentation listener failed to start. The gateway is unaffected.",
        error));
}

/// <summary>An immutable reading of the documentation listener's state.</summary>
/// <param name="Status">What the listener is doing.</param>
/// <param name="ListeningUrl">The address actually bound, or null.</param>
/// <param name="Detail">A sentence for an operator.</param>
/// <param name="Error">The failure, when <see cref="Status"/> is <see cref="DocumentationSiteStatus.Failed"/>.</param>
public sealed record DocumentationSiteSnapshot(
    DocumentationSiteStatus Status,
    string? ListeningUrl,
    string Detail,
    string? Error);

/// <summary>What the documentation listener is doing.</summary>
public enum DocumentationSiteStatus
{
    /// <summary>Configured, but the host has not started it yet.</summary>
    NotStarted,

    /// <summary>Deliberately not running — switched off, or nothing to bind in this process.</summary>
    Disabled,

    /// <summary>Bound and serving.</summary>
    Listening,

    /// <summary>Tried to start and could not. The gateway carries on regardless.</summary>
    Failed,
}
