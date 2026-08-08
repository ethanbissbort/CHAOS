using System.Collections.ObjectModel;

namespace Chaos.Host.Routing;

/// <summary>
/// The compiled-in route-ownership manifest: the shipped answer to "who serves
/// what" before any node-local configuration is applied.
/// </summary>
/// <remarks>
/// <para>
/// Today every <c>/api/v1/*</c> prefix is <see cref="RouteOwner.Python"/>. The
/// prefixes mirror the FastAPI routers in <c>src/chaos/api/routers/</c>
/// and the endpoint reference in <c>docs/api.md</c>, one row per subsystem, so a
/// port is a one-row change rather than a surgical edit of a catch-all.
/// </para>
/// <para>
/// The <c>/api/v1</c> row is a deliberate catch-all: an endpoint added to the
/// Python app that nobody remembered to list here still proxies correctly
/// instead of 404-ing. Ownership never silently moves to .NET — only an
/// explicit row can do that, and only if an endpoint backs it.
/// </para>
/// </remarks>
public static class RouteOwnershipDefaults
{
    private const string PythonToday = "Served by the Python/FastAPI backend. Not yet ported.";

    /// <summary>
    /// The shipped manifest. Ordering here is for readability only; matching is
    /// longest-prefix-wins regardless of declaration order.
    /// </summary>
    public static readonly IReadOnlyList<RouteOwnership> Manifest = new ReadOnlyCollection<RouteOwnership>(
    [
        // ---------------------------------------------------------------
        // Gateway-native. These are the only .NET-owned rows today, and each
        // is backed by an endpoint registered in HostEndpoints.
        // ---------------------------------------------------------------
        new RouteOwnership
        {
            PathPrefix = "/health",
            Owner = RouteOwner.Dotnet,
            PortedInVersion = "0.5.0",
            Notes = "Gateway health. Answers even when the backend is down, and reports backend "
                  + "reachability honestly. The Python backend's own /health is polled by the "
                  + "gateway rather than proxied, so that a dead backend cannot make /health silent.",
        },
        new RouteOwnership
        {
            PathPrefix = "/host",
            Owner = RouteOwner.Dotnet,
            PortedInVersion = "0.5.0",
            Notes = "Gateway introspection: /host/info and /host/routes. Never proxied.",
        },

        // ---------------------------------------------------------------
        // Python-owned API surface (docs/api.md).
        // ---------------------------------------------------------------
        new RouteOwnership
        {
            PathPrefix = "/api/v1",
            Owner = RouteOwner.Python,
            Notes = "Catch-all for the platform API. Any /api/v1 route not claimed by a more "
                  + "specific row proxies to Python, so a new Python endpoint is reachable "
                  + "without a gateway change.",
        },
        Python("/api/v1/overview", "Property overview, subsystem roll-up, GeoJSON map, SDD 17.4 control presentation."),
        Python("/api/v1/assets", "Asset registry."),
        Python("/api/v1/points", "Point instances, plus the /points/{point_id}/current and /history telemetry paths."),
        Python("/api/v1/registry", "Registry summary, dictionaries, design conflicts, reload."),
        Python("/api/v1/telemetry", "Current state, history, stale points, dead letters, ingest stats, simulate."),
        Python("/api/v1/commands", "Audited supervisory command path. Interlocks and audit live in Python; "
                                 + "porting this row moves a safety-relevant path and is not a routine flip."),
        Python("/api/v1/operating-modes", "SDD 11 operating modes."),
        Python("/api/v1/audit", "Control audit trail."),
        Python("/api/v1/energy", "EMS state machine, budgets, leases, loads, shed actions, dashboard."),
        Python("/api/v1/alarms", "Alarm lifecycle and definitions."),
        Python("/api/v1/incidents", "Correlated incidents."),
        Python("/api/v1/notifications", "Notification delivery log."),
        Python("/api/v1/annunciator", "Annunciator panel state (SDD 14, 17.3)."),
        Python("/api/v1/maintenance", "Maintenance plans, due work, inspections, calibrations, spare parts."),
        Python("/api/v1/work-orders", "Work orders."),
        Python("/api/v1/commissioning", "SDD 19 commissioning records and binding enablement."),

        // ---------------------------------------------------------------
        // Platform surface that is not /api/v1 but still belongs to Python.
        // The OpenAPI schema is authoritative per docs/api.md, so it must stay
        // reachable through the gateway.
        // ---------------------------------------------------------------
        Python("/openapi.json", "The authoritative API schema. docs/api.md defers to it."),
        Python("/docs", "Swagger UI."),
        Python("/redoc", "ReDoc."),
    ]);

    private static RouteOwnership Python(string prefix, string notes) => new()
    {
        PathPrefix = prefix,
        Owner = RouteOwner.Python,
        Notes = $"{notes} {PythonToday}",
    };
}
