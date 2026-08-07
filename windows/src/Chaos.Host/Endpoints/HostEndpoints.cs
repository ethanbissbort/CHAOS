using Chaos.Host.Abstractions;
using Chaos.Host.Configuration;
using Chaos.Host.Health;
using Chaos.Host.Routing;
using Chaos.Host.Web;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Endpoints;

/// <summary>
/// The gateway's own endpoints: <c>/health</c>, <c>/health/live</c>,
/// <c>/host/info</c> and <c>/host/routes</c>.
/// </summary>
/// <remarks>
/// These are the only .NET-owned routes today, and each is backed by a row in
/// the route-ownership manifest. Adding an endpoint here without a manifest row
/// is harmless; adding a manifest row without an endpoint stops the host from
/// starting.
/// </remarks>
public static class HostEndpoints
{
    /// <summary>Maps the gateway's endpoints.</summary>
    /// <param name="endpoints">The endpoint route builder.</param>
    /// <returns>The builder, for chaining.</returns>
    public static IEndpointRouteBuilder MapChaosHostEndpoints(this IEndpointRouteBuilder endpoints)
    {
        ArgumentNullException.ThrowIfNull(endpoints);

        endpoints.MapGet("/health", GetHealth).ExcludeFromDescription();
        endpoints.MapGet("/health/live", GetLiveness).ExcludeFromDescription();
        endpoints.MapGet("/host/info", GetInfo).ExcludeFromDescription();
        endpoints.MapGet("/host/routes", GetRoutes).ExcludeFromDescription();

        return endpoints;
    }

    /// <summary>
    /// Gateway health, including an honest account of the backend behind it.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Always answers, including when the backend is unreachable — that is the
    /// case it exists for.
    /// </para>
    /// <para>
    /// <b>Status codes.</b> 200 only when the backend is <c>up</c>; 503 when it
    /// is <c>down</c> or <c>starting</c>, with the full body either way. A
    /// gateway that returns 200 while the platform behind it is dead tells every
    /// status-code-only monitor that an off-grid site with no alarm engine is
    /// fine. Use <c>/health/live</c> to ask only whether this process is alive.
    /// </para>
    /// </remarks>
    private static IResult GetHealth(HttpContext context)
    {
        var services = context.RequestServices;
        var health = services.GetRequiredService<BackendHealthState>();
        var options = services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;
        var supervisor = services.GetRequiredService<IBackendSupervisor>();

        var now = DateTimeOffset.UtcNow;
        var report = BackendHealthReport.Create(health, options, now);
        var snapshot = report.Snapshot;

        var versionMismatch = !report.Stale && snapshot.VersionCompatible == false;
        var status = report.Backend switch
        {
            "up" when versionMismatch => "degraded",
            "up" => "ok",
            "starting" => "starting",
            _ => "degraded",
        };

        var payload = new
        {
            status,
            host = new
            {
                component = "Chaos.Host",
                version = HostVersion.Version,
                startedUtc = health.CreatedUtc,
                uptimeSeconds = Math.Round((now - health.CreatedUtc).TotalSeconds, 1),
            },

            // The contract field: "up" | "down" | "starting".
            backend = report.Backend,
            backendDetail = new
            {
                url = options.BackendUrl,
                detail = report.Detail,
                stale = report.Stale,
                lastError = snapshot.LastError,
                lastCheckedUtc = snapshot.LastCheckedUtc,
                lastSuccessUtc = snapshot.LastSuccessUtc,
                consecutiveFailures = snapshot.ConsecutiveFailures,
                lastStatusCode = snapshot.LastStatusCode,
                reportedVersion = snapshot.BackendVersion,
                versionCompatible = snapshot.VersionCompatible,
                platformVersionContract = HostVersion.PlatformVersionContract,
            },
            supervisor = DescribeSupervisor(supervisor),
        };

        return Results.Json(
            payload,
            statusCode: report.BackendIsUp && !versionMismatch
                ? StatusCodes.Status200OK
                : StatusCodes.Status503ServiceUnavailable);
    }

    /// <summary>
    /// Process liveness only. 200 whenever this host is running, regardless of
    /// the backend. Separate from <c>/health</c> so "the gateway process is
    /// alive" and "the platform is serving" are never conflated.
    /// </summary>
    private static IResult GetLiveness(HttpContext context)
    {
        var health = context.RequestServices.GetRequiredService<BackendHealthState>();
        return Results.Json(new
        {
            status = "ok",
            component = "Chaos.Host",
            version = HostVersion.Version,
            startedUtc = health.CreatedUtc,
            detail = "This reports only that the gateway process is running. It says nothing about the platform "
                   + "backend; GET /health does.",
        });
    }

    /// <summary>Host identity, configuration and migration summary.</summary>
    private static IResult GetInfo(HttpContext context)
    {
        var services = context.RequestServices;
        var options = services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;
        var table = services.GetRequiredService<RouteOwnershipTable>();
        var supervisor = services.GetRequiredService<IBackendSupervisor>();
        var webRoot = services.GetRequiredService<WebRootResolution>();
        var health = services.GetRequiredService<BackendHealthState>();
        var report = BackendHealthReport.Create(health, options, DateTimeOffset.UtcNow);
        var snapshot = report.Snapshot;

        return Results.Json(new
        {
            product = "Project CHAOS",
            component = "Chaos.Host",
            description = "The .NET gateway: the only LAN listener, reverse proxy to the Python platform backend, "
                        + "host of the operator console, and the home of ported subsystems.",
            version = HostVersion.Version,
            platformVersionContract = HostVersion.PlatformVersionContract,
            runtime = new
            {
                framework = Environment.Version.ToString(),
                description = System.Runtime.InteropServices.RuntimeInformation.FrameworkDescription,
                os = System.Runtime.InteropServices.RuntimeInformation.OSDescription,
                architecture = System.Runtime.InteropServices.RuntimeInformation.OSArchitecture.ToString(),
                processId = Environment.ProcessId,
                machineName = Environment.MachineName,
            },
            listenUrl = options.ListenUrl,
            backendUrl = options.BackendUrl,
            backend = new
            {
                status = report.Backend,
                stale = report.Stale,
                detail = report.Detail,
                reportedVersion = snapshot.BackendVersion,
                versionCompatible = snapshot.VersionCompatible,
                lastError = snapshot.LastError,
                lastSuccessUtc = snapshot.LastSuccessUtc,
            },
            webRoot = new
            {
                present = webRoot.Present,
                path = webRoot.Path,
                source = webRoot.Source,
                searched = webRoot.SearchedPaths,
            },
            windowsService = new
            {
                running = WindowsServiceIntegration.IsRunningAsWindowsService(),
                supported = OperatingSystem.IsWindows(),
                detail = OperatingSystem.IsWindows()
                    ? "Windows Service lifetime is enabled when the process is started by the service control manager."
                    : "Not running on Windows; the Windows Service lifetime is not registered.",
            },
            supervisor = DescribeSupervisor(supervisor),
            routes = new
            {
                total = table.Entries.Count,
                python = table.PythonCount,
                dotnet = table.DotnetCount,
                detail = "/host/routes",
            },
            startedUtc = health.CreatedUtc,
        });
    }

    /// <summary>
    /// The live route-ownership manifest: who serves what, what has been ported,
    /// what is still proxied.
    /// </summary>
    private static IResult GetRoutes(HttpContext context)
    {
        var services = context.RequestServices;
        var table = services.GetRequiredService<RouteOwnershipTable>();
        var inventory = services.GetRequiredService<NativeRouteInventory>();
        var options = services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;

        var rows = table.Entries.Select(entry => new
        {
            pathPrefix = entry.PathPrefix,
            owner = entry.Owner.ToString(),
            source = entry.Source.ToString(),
            portedInVersion = entry.PortedInVersion,
            notes = entry.Notes,
            target = entry.Owner == RouteOwner.Python ? options.BackendUrl : "Chaos.Host (in process)",
            nativeEndpoints = entry.Owner == RouteOwner.Dotnet
                ? inventory.PatternsUnder(entry.PathPrefix)
                : Array.Empty<string>(),
        });

        return Results.Json(new
        {
            generatedUtc = DateTimeOffset.UtcNow,
            hostVersion = HostVersion.Version,
            platformVersionContract = HostVersion.PlatformVersionContract,
            matching = "Longest prefix wins, at segment boundaries, ordinal case-insensitive. "
                     + "A path matching no row is not proxied: it falls through to this host's own pipeline "
                     + "(operator console, then 404).",
            summary = new
            {
                total = table.Entries.Count,
                python = table.PythonCount,
                dotnet = table.DotnetCount,
                portedPercent = table.Entries.Count == 0
                    ? 0d
                    : Math.Round(100d * table.DotnetCount / table.Entries.Count, 1),
            },
            backendUrl = options.BackendUrl,
            routes = rows,
            nativeEndpoints = inventory.Patterns,
            notOwnedByTheManifest = new[]
            {
                "/ and /ui/** - the operator console, served from this host's static-file pipeline.",
            },
        });
    }

    private static object DescribeSupervisor(IBackendSupervisor supervisor)
    {
        var status = supervisor.Status;
        var isNull = supervisor is NullBackendSupervisor;

        return new
        {
            registered = !isNull,
            implementation = isNull ? null : supervisor.GetType().FullName,
            state = status.State.ToString(),
            detail = status.Detail,
            lastError = status.LastError,
            processId = status.ProcessId,
            startedUtc = status.StartedUtc,
            lastExitCode = status.LastExitCode,
            restartCount = status.RestartCount,
        };
    }
}
