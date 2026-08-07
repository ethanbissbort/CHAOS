namespace Chaos.Host.Health;

/// <summary>
/// An immutable reading of backend reachability, as observed by the gateway's
/// health poller.
/// </summary>
/// <remarks>
/// Nullable fields mean "not known yet" and are rendered as JSON nulls. Nothing
/// here is defaulted to a value that reads as healthy.
/// </remarks>
/// <param name="Reachability">Whether the backend is answering.</param>
/// <param name="LastCheckedUtc">When the last poll completed, or null if none has.</param>
/// <param name="LastSuccessUtc">When the backend last answered successfully, or null if it never has.</param>
/// <param name="LastError">Why the last poll failed, or null if the last poll succeeded / none has run.</param>
/// <param name="LastStatusCode">HTTP status of the last poll that produced a response, or null.</param>
/// <param name="ConsecutiveFailures">Failed polls since the last success.</param>
/// <param name="BackendVersion">The <c>version</c> the backend reports on its health endpoint, or null if unknown.</param>
/// <param name="VersionCompatible">
/// Whether <paramref name="BackendVersion"/> matches the platform version
/// contract this host was built against, on major.minor. Null when the backend
/// version is unknown — an unknown version is not a match and not a mismatch.
/// </param>
public sealed record BackendHealthSnapshot(
    BackendReachability Reachability,
    DateTimeOffset? LastCheckedUtc,
    DateTimeOffset? LastSuccessUtc,
    string? LastError,
    int? LastStatusCode,
    int ConsecutiveFailures,
    string? BackendVersion,
    bool? VersionCompatible)
{
    /// <summary>The reading before any poll has run.</summary>
    public static readonly BackendHealthSnapshot Initial = new(
        BackendReachability.Starting,
        LastCheckedUtc: null,
        LastSuccessUtc: null,
        LastError: null,
        LastStatusCode: null,
        ConsecutiveFailures: 0,
        BackendVersion: null,
        VersionCompatible: null);

    /// <summary>
    /// The wire value used for the <c>backend</c> field of <c>GET /health</c>:
    /// <c>"up"</c>, <c>"down"</c> or <c>"starting"</c>.
    /// </summary>
    public string BackendWireValue => Reachability switch
    {
        BackendReachability.Up => "up",
        BackendReachability.Down => "down",
        _ => "starting",
    };
}
