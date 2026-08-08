using Chaos.Host.Abstractions;
using Chaos.Host.Configuration;
using Chaos.Host.Docs;
using Chaos.Host.Health;
using Chaos.Host.Http;
using Chaos.Host.Routing;
using Chaos.Host.Setup;
using Chaos.Host.Web;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Endpoints;

/// <summary>
/// The gateway's own endpoints: <c>/health</c>, <c>/health/live</c>,
/// <c>/host/info</c>, <c>/host/routes</c>, <c>/host/setup</c> and
/// <c>/host/setup/run</c>.
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
        endpoints.MapGet("/host/setup", GetSetup).ExcludeFromDescription();
        endpoints.MapPost("/host/setup/run", PostSetupRun).ExcludeFromDescription();

        return endpoints;
    }

    /// <summary>
    /// First-run setup state: what the gateway found, what it did, and what an
    /// operator has to do next if anything.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <b>Always 200.</b> This is a report, and a report that a machine needs
    /// setting up is a successful report. The state is in the body, in
    /// <c>state</c>: <c>not_started</c>, <c>checking</c>, <c>running</c>,
    /// <c>ready</c>, <c>failed</c> or <c>needs_attention</c>. Health is
    /// <c>/health</c>'s job, and it carries this state too.
    /// </para>
    /// <para>
    /// Safe to poll: it reads an immutable snapshot and never touches the
    /// database.
    /// </para>
    /// </remarks>
    private static IResult GetSetup(HttpContext context)
    {
        var services = context.RequestServices;
        var coordinator = services.GetRequiredService<PlatformSetupCoordinator>();
        var options = services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;

        return Results.Json(SetupPayload.Create(
            coordinator.Snapshot,
            options,
            coordinator.RecordPath,
            DateTimeOffset.UtcNow));
    }

    /// <summary>
    /// Runs setup now — the shell's "Set up now" and "Retry" button.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Idempotent and safe to call twice. On an already-set-up machine the run
    /// completes as a no-op and says so; it never re-imports. While a run is in
    /// flight a second call is declined rather than queued, so two clicks cannot
    /// produce two concurrent imports.
    /// </para>
    /// <para>
    /// <b>Status codes.</b> 202 when this call started a run; 200 when it did
    /// not, with <c>accepted: false</c> and a <c>reason</c> of
    /// <c>already_running</c> or <c>auto_setup_disabled</c>. Both carry the full
    /// <c>/host/setup</c> body under <c>setup</c>, so the shell needs one round
    /// trip rather than two.
    /// </para>
    /// <para>
    /// <c>?force=true</c> proceeds when automatic setup is off, or when the
    /// database was assessed as needing attention. It never makes setup
    /// destructive: the same <c>init-db</c> and <c>load-all --skip-missing</c>
    /// run either way, and neither drops anything.
    /// </para>
    /// </remarks>
    private static IResult PostSetupRun(HttpContext context)
    {
        var services = context.RequestServices;
        var coordinator = services.GetRequiredService<PlatformSetupCoordinator>();
        var options = services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;

        var force = QueryFlags.IsTrue(context.Request.Query["force"]);
        var acceptance = coordinator.RequestRun("api", force);

        return Results.Json(
            SetupPayload.ForRun(
                acceptance,
                force,
                coordinator.Snapshot,
                options,
                coordinator.RecordPath,
                DateTimeOffset.UtcNow),
            statusCode: acceptance.Accepted
                ? StatusCodes.Status202Accepted
                : StatusCodes.Status200OK);
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
    /// <para>
    /// <b>Setup counts too.</b> A gateway reporting healthy over a platform with
    /// no database is the same lie in a different costume, so a setup state of
    /// <c>failed</c> or <c>needs_attention</c> forces <c>degraded</c>, and
    /// <c>checking</c> or <c>running</c> forces <c>starting</c>, whatever the
    /// backend says. <c>not_started</c> forces nothing: it means the gateway has
    /// not looked, which is not evidence either way, and the backend probe is
    /// already reporting whether the platform answers. The <c>setup</c> object
    /// is additive — every field that was on this payload before is still here,
    /// unchanged.
    /// </para>
    /// </remarks>
    private static IResult GetHealth(HttpContext context)
    {
        var services = context.RequestServices;
        var health = services.GetRequiredService<BackendHealthState>();
        var options = services.GetRequiredService<IOptions<ChaosHostOptions>>().Value;
        var supervisor = services.GetRequiredService<IBackendSupervisor>();
        var setup = services.GetRequiredService<PlatformSetupCoordinator>().Snapshot;

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

        // Setup can only ever make the answer worse, never better: a ready
        // database does not redeem an unreachable backend.
        var fromSetup = SetupPayload.HealthContribution(setup.State);
        if (fromSetup == "degraded" || (fromSetup == "starting" && status == "ok"))
        {
            status = fromSetup;
        }

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
            setup = SetupPayload.ForHealth(setup),
        };

        return Results.Json(
            payload,
            statusCode: status == "ok"
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
        var documentation = services.GetRequiredService<DocumentationSiteState>();
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

            // Where the manuals are. The shell and the console link to this, so
            // an operator never has to remember a second port number.
            documentation = DocumentationPayload.Create(documentation, context.Request.Host),
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
                "The documentation site - a SEPARATE listener on its own port (see documentation on /host/info). "
              + "It is not reachable through this listener at all, so no manifest row could describe it.",
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
