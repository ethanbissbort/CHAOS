using Chaos.Host.Abstractions;
using Chaos.Host.Configuration;
using Chaos.Host.Docs;
using Chaos.Host.Endpoints;
using Chaos.Host.Health;
using Chaos.Host.Proxy;
using Chaos.Host.Routing;
using Chaos.Host.Setup;
using Chaos.Host.Web;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Options;

namespace Chaos.Host;

/// <summary>
/// Composition root for the CHAOS gateway.
/// </summary>
/// <remarks>
/// Split from <c>Program.cs</c> so the whole host can be assembled from a test,
/// from the WinUI shell's in-process host, or from any other entry point,
/// without duplicating registration order — which matters here, because the
/// order hosted services run in is part of the design.
/// </remarks>
public static class ChaosHostExtensions
{
    /// <summary>
    /// Registers everything the gateway needs and applies
    /// <see cref="ChaosHostOptions.ListenUrl"/> to the web host.
    /// </summary>
    /// <param name="builder">The application builder.</param>
    /// <returns>The builder, for chaining.</returns>
    /// <exception cref="RouteOwnershipException">The route-ownership manifest is malformed or ambiguous.</exception>
    public static WebApplicationBuilder AddChaosHost(this WebApplicationBuilder builder)
    {
        ArgumentNullException.ThrowIfNull(builder);

        // appsettings' "Chaos" section, overridden by CHAOS_-prefixed
        // environment variables at the same level: CHAOS_BackendUrl overrides
        // Chaos:BackendUrl, CHAOS_Routes__0__Owner overrides Chaos:Routes:0:Owner.
        var chaosConfiguration = new ConfigurationBuilder()
            .AddConfiguration(builder.Configuration.GetSection(ChaosHostOptions.SectionName))
            .AddEnvironmentVariables(ChaosHostOptions.EnvironmentPrefix)
            .Build();

        builder.Services.AddSingleton<IValidateOptions<ChaosHostOptions>, ChaosHostOptionsValidator>();
        builder.Services
            .AddOptions<ChaosHostOptions>()
            .Bind(chaosConfiguration)
            .ValidateOnStart();

        // The table has to exist before the pipeline is built, so it is bound
        // eagerly here rather than resolved from IOptions later.
        var options = new ChaosHostOptions();
        chaosConfiguration.Bind(options);

        var table = RouteOwnershipTable.Build(
            RouteOwnershipDefaults.Manifest,
            [.. options.Routes],
            options.ReplaceDefaultRoutes);

        builder.Services.AddSingleton(table);
        builder.Services.AddSingleton<NativeRouteInventory>();
        builder.Services.AddSingleton<BackendHealthState>();
        builder.Services.AddSingleton<BackendForwarder>();

        var webRoot = WebRootResolver.Resolve(options, builder.Environment.ContentRootPath);
        builder.Services.AddSingleton(webRoot);

        // The documentation site is resolved here, not when the listener starts,
        // so /host/info can report where the gateway looked even in a process
        // where the listener never runs.
        var documentationRoot = DocumentationRootResolver.Resolve(
            options.Docs,
            webRoot,
            builder.Environment.ContentRootPath);
        builder.Services.AddSingleton(documentationRoot);
        builder.Services.AddSingleton(new DocumentationSiteState(options.Docs, documentationRoot));

        builder.Services.AddHttpForwarder();
        builder.Services
            .AddHttpClient(BackendHealthMonitor.HttpClientName)
            .ConfigureHttpClient(client =>
            {
                // The per-poll budget is enforced by a CancellationToken so a
                // timeout is distinguishable from a transport failure.
                client.Timeout = Timeout.InfiniteTimeSpan;
            });

        builder.Services.Configure<HostOptions>(host =>
        {
            host.ShutdownTimeout = options.ShutdownTimeout;

            // A background service that throws must not silently take the whole
            // host with it; the gateway's job during a failure is to stay up and
            // report it.
            host.BackgroundServiceExceptionBehavior = BackgroundServiceExceptionBehavior.Ignore;
        });

        // Order is deliberate:
        //  1. validate the manifest - refuse to start on a route that would 404;
        //  2. start first-run setup, so a fresh machine is being prepared while
        //     the backend is starting rather than after it has already failed
        //     against an empty database. It does not block startup - see
        //     SetupHostedService - so the shell can poll /host/setup and watch;
        //  3. start the backend (if a supervisor is registered);
        //  4. start polling the backend.
        //  5. start the documentation listener last. It is a second, read-only
        //     listener on its own port and nothing else in this process depends
        //     on it, so it goes at the back of the queue and cannot delay
        //     anything that matters more.
        builder.Services.AddHostedService<RouteOwnershipStartupCheck>();
        builder.Services.AddPlatformSetup();
        builder.Services.TryAddSingleton<IBackendSupervisor, NullBackendSupervisor>();
        builder.Services.AddHostedService<Supervision.BackendSupervisorHost>();
        builder.Services.AddHostedService<BackendHealthMonitor>();
        builder.Services.AddHostedService<DocumentationSiteHost>();

        WindowsServiceIntegration.AddWindowsServiceIfAvailable(builder);

        if (!string.IsNullOrWhiteSpace(options.ListenUrl))
        {
            builder.WebHost.UseUrls(
                options.ListenUrl.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        }

        return builder;
    }

    /// <summary>
    /// Builds the request pipeline: ownership fork, operator console, gateway
    /// endpoints, then captures the endpoint inventory for startup validation.
    /// </summary>
    /// <param name="app">The application.</param>
    /// <returns>The application, for chaining.</returns>
    public static WebApplication UseChaosHost(this WebApplication app)
    {
        ArgumentNullException.ThrowIfNull(app);

        var inventory = app.Services.GetRequiredService<NativeRouteInventory>();
        var webRoot = app.Services.GetRequiredService<WebRootResolution>();

        // First real decision in the pipeline. Python-owned paths never reach
        // the static-file middleware or the endpoint pipeline.
        app.UseMiddleware<RouteOwnershipMiddleware>();

        app.UseOperatorConsole(webRoot);

        app.UseRouting();
        app.MapChaosHostEndpoints();

        // Everything that will ever be mapped has been mapped; freeze the list
        // the startup check validates the manifest against.
        inventory.Capture(app);

        return app;
    }
}
