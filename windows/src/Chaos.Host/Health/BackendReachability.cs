namespace Chaos.Host.Health;

/// <summary>
/// Whether the Python backend is actually answering HTTP, as measured by the
/// gateway's own polling.
/// </summary>
/// <remarks>
/// Distinct from <see cref="Abstractions.BackendState"/>, which is what the
/// process supervisor believes. A running process that is not yet serving is a
/// real state, and so is a process the gateway does not manage but can reach.
/// </remarks>
public enum BackendReachability
{
    /// <summary>
    /// No successful poll yet, and still inside the start window. Reported as
    /// <c>starting</c>. Never reported as up: we have no evidence.
    /// </summary>
    Starting = 0,

    /// <summary>The last poll succeeded. Reported as <c>up</c>.</summary>
    Up = 1,

    /// <summary>Polling is failing and the start window has expired. Reported as <c>down</c>.</summary>
    Down = 2,
}
