using Chaos.Host.Configuration;

namespace Chaos.Host.Health;

/// <summary>
/// What <c>GET /health</c> reports about the backend, after accounting for the
/// possibility that the reading itself is out of date.
/// </summary>
/// <param name="Snapshot">The raw reading from the poller.</param>
/// <param name="Backend">The <c>backend</c> field: <c>"up"</c>, <c>"down"</c> or <c>"starting"</c>.</param>
/// <param name="Stale">Whether the reading is too old to be trusted.</param>
/// <param name="Detail">
/// An explanation when something is not straightforwardly healthy, or null when
/// there is nothing to add.
/// </param>
public sealed record BackendHealthReport(
    BackendHealthSnapshot Snapshot,
    string Backend,
    bool Stale,
    string? Detail)
{
    /// <summary>Whether the backend is confirmed to be answering right now.</summary>
    public bool BackendIsUp => Backend == "up";

    /// <summary>
    /// Derives the reported state, downgrading a reading the poller has stopped
    /// refreshing.
    /// </summary>
    /// <param name="state">The live health state.</param>
    /// <param name="options">Gateway options, for the poll interval and start window.</param>
    /// <param name="nowUtc">The current time. Injectable for tests.</param>
    /// <returns>The report.</returns>
    /// <remarks>
    /// <para>
    /// A frozen poller is the failure this handles. If the monitor stops
    /// updating — a crash, a deadlock, a thread-pool starvation — the last
    /// snapshot would otherwise be served forever, and a stale <c>"up"</c> is
    /// precisely the reassuring lie this platform must not tell.
    /// </para>
    /// <para>
    /// So a reading older than three poll intervals (minimum 30 seconds) is
    /// reported as <c>down</c> with <c>stale: true</c> and an explanation.
    /// "We cannot confirm the platform is answering" is operationally the same
    /// instruction as "do not trust it".
    /// </para>
    /// </remarks>
    public static BackendHealthReport Create(BackendHealthState state, ChaosHostOptions options, DateTimeOffset nowUtc)
    {
        ArgumentNullException.ThrowIfNull(state);
        ArgumentNullException.ThrowIfNull(options);

        var snapshot = state.Current;
        var staleAfter = Max(options.BackendHealthInterval * 3, TimeSpan.FromSeconds(30));

        if (snapshot.LastCheckedUtc is null)
        {
            // No poll has completed. Inside the start window that is "starting";
            // beyond it, the poller should have reported by now and its silence
            // is itself the finding.
            var graceExpiresUtc = state.CreatedUtc + options.BackendStartTimeout + staleAfter;
            return nowUtc <= graceExpiresUtc
                ? new BackendHealthReport(snapshot, "starting", Stale: false,
                    "No backend health check has completed yet.")
                : new BackendHealthReport(snapshot, "down", Stale: true,
                    $"No backend health check has completed since this host started at {state.CreatedUtc:O}. "
                  + "The backend health poller is not reporting, so backend state is unknown and is reported as "
                  + "down rather than assumed good.");
        }

        var age = nowUtc - snapshot.LastCheckedUtc.Value;
        if (age > staleAfter)
        {
            return new BackendHealthReport(snapshot, "down", Stale: true,
                $"The last backend health check completed {age.TotalSeconds:F0}s ago, which is beyond the "
              + $"{staleAfter.TotalSeconds:F0}s staleness limit. This reading is not current, so backend state is "
              + "unknown and is reported as down rather than assumed good.");
        }

        var detail = snapshot.Reachability switch
        {
            BackendReachability.Up when snapshot.VersionCompatible == false =>
                $"The backend reports platform version {snapshot.BackendVersion}, which does not satisfy this "
              + $"host's contract {HostVersion.PlatformVersionContract}. The route-ownership manifest was written "
              + "against the contract version; the API surface may differ from what this gateway assumes.",
            BackendReachability.Up => null,
            BackendReachability.Starting => "The backend has not answered yet and is still inside its start window.",
            _ => "The backend is not answering. The gateway is up; the platform behind it is not.",
        };

        return new BackendHealthReport(snapshot, snapshot.BackendWireValue, Stale: false, detail);
    }

    private static TimeSpan Max(TimeSpan left, TimeSpan right) => left > right ? left : right;
}
