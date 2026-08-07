namespace Chaos.Host.Supervisor.Events;

/// <summary>
/// Service lifecycle events, with stable numeric IDs so an operator can filter
/// the Windows Event Log on them and a monitoring rule can key off them.
/// </summary>
/// <remarks>
/// These numbers are a contract with whoever writes the monitoring rules.
/// Add to the end; never renumber.
/// </remarks>
public enum ChaosEventId
{
    /// <summary>Supervision requested. The backend is not up yet.</summary>
    SupervisorStarting = 1000,

    /// <summary>Supervision stopped on request; the backend is down deliberately.</summary>
    SupervisorStopped = 1001,

    /// <summary>Which Python was chosen, and where it came from.</summary>
    RuntimeResolved = 1010,

    /// <summary>No usable Python. Terminal.</summary>
    RuntimeUnresolvable = 1011,

    /// <summary>A backend process was launched. It has not answered /health yet.</summary>
    BackendLaunched = 1020,

    /// <summary>/health answered. This is the only event that means "serving".</summary>
    BackendReady = 1021,

    /// <summary>The backend exited when it should have been running.</summary>
    BackendExited = 1022,

    /// <summary>The process is alive but /health has stopped answering.</summary>
    BackendUnhealthy = 1023,

    /// <summary>A restart has been scheduled after backoff.</summary>
    BackendRestarting = 1024,

    /// <summary>The backend never became ready within the readiness timeout.</summary>
    BackendReadinessTimeout = 1025,

    /// <summary>Too many failures in the window. Retrying has stopped. Terminal.</summary>
    CircuitBreakerTripped = 1030,

    /// <summary>Graceful shutdown was not possible or timed out; the tree was killed.</summary>
    ForcedTermination = 1040,

    /// <summary>The Event Log itself is unavailable — recorded through ILogger.</summary>
    EventLogUnavailable = 1090,
}

/// <summary>Severity as the Windows Event Log understands it.</summary>
public enum ChaosEventLevel
{
    Information = 0,
    Warning = 1,
    Error = 2,
}

/// <summary>
/// Where service lifecycle events go. One implementation writes the Windows
/// Event Log (plus <c>ILogger</c>); the other writes <c>ILogger</c> only.
/// </summary>
public interface IChaosEventSink
{
    /// <summary>
    /// Where events are actually going, in words. Logged at startup so nobody
    /// goes looking in the Event Viewer for entries that were never written.
    /// </summary>
    string Description { get; }

    void Write(ChaosEventId id, ChaosEventLevel level, string message);
}
