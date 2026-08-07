using Chaos.Host.Abstractions;
using Chaos.Host.Supervisor.Events;
using Chaos.Host.Supervisor.Health;
using Chaos.Host.Supervisor.Processes;
using Chaos.Host.Supervisor.Runtime;
using Chaos.Host.Supervisor.Time;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Supervisor;

/// <summary>Wires the backend supervisor into the gateway with one call.</summary>
public static class ServiceCollectionExtensions
{
    /// <summary>
    /// Registers the supervisor, its collaborators and (unless turned off) the
    /// hosted service that starts and stops it with the host.
    /// </summary>
    /// <remarks>
    /// Every collaborator is registered with <c>TryAdd</c>, so a host that has
    /// already registered its own clock, launcher or health probe keeps it.
    /// </remarks>
    public static IServiceCollection AddChaosBackendSupervisor(
        this IServiceCollection services,
        Action<BackendSupervisorOptions>? configure = null)
    {
        ArgumentNullException.ThrowIfNull(services);

        var builder = services.AddOptions<BackendSupervisorOptions>();
        if (configure is not null)
        {
            builder.Configure(configure);
        }

        builder.Validate(
            options => options.Validate().Count == 0,
            "The backend supervisor options are invalid. Call BackendSupervisorOptions.Validate() for the list.");

        return AddCore(services);
    }

    /// <summary>
    /// Binds options from configuration (by convention
    /// <see cref="BackendSupervisorOptions.SectionName"/>) before applying
    /// <paramref name="configure"/>.
    /// </summary>
    public static IServiceCollection AddChaosBackendSupervisor(
        this IServiceCollection services,
        IConfiguration configuration,
        Action<BackendSupervisorOptions>? configure = null)
    {
        ArgumentNullException.ThrowIfNull(services);
        ArgumentNullException.ThrowIfNull(configuration);

        var builder = services.AddOptions<BackendSupervisorOptions>().Bind(configuration);
        if (configure is not null)
        {
            builder.Configure(configure);
        }

        builder.Validate(
            options => options.Validate().Count == 0,
            "The backend supervisor options are invalid. Call BackendSupervisorOptions.Validate() for the list.");

        return AddCore(services);
    }

    private static IServiceCollection AddCore(IServiceCollection services)
    {
        services.TryAddSingleton<IFileSystem>(PhysicalFileSystem.Instance);
        services.TryAddSingleton<IClock>(SystemClock.Instance);
        services.TryAddSingleton<IPythonRuntimeResolver>(
            provider => new PythonRuntimeResolver(provider.GetRequiredService<IFileSystem>()));
        services.TryAddSingleton<IProcessLauncher>(
            provider => new SystemProcessLauncher(provider.GetRequiredService<ILogger<SystemProcessLauncher>>()));

        // One long-lived client against one fixed loopback endpoint: no DNS
        // rotation to worry about and no socket churn from a per-probe client.
        services.TryAddSingleton<IBackendHealthProbe>(_ => new HttpBackendHealthProbe());

        services.TryAddSingleton<IChaosEventSink>(provider => ChaosEventSinkFactory.Create(
            provider.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value,
            provider.GetRequiredService<ILoggerFactory>().CreateLogger("Chaos.Host.Supervisor.Lifecycle")));

        services.TryAddSingleton<BackendSupervisor>();
        services.TryAddSingleton<IBackendSupervisor>(provider => provider.GetRequiredService<BackendSupervisor>());
        services.TryAddSingleton<IBackendSupervisorDiagnostics>(
            provider => provider.GetRequiredService<BackendSupervisor>());

        services.AddSingleton<IHostedService>(provider =>
        {
            var options = provider.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value;
            return options.RegisterHostedService
                ? new BackendSupervisorHostedService(provider.GetRequiredService<BackendSupervisor>())
                : NullHostedService.Instance;
        });

        return services;
    }
}

/// <summary>Starts and stops the supervisor with the host.</summary>
public sealed class BackendSupervisorHostedService(BackendSupervisor supervisor) : IHostedService
{
    private readonly BackendSupervisor _supervisor =
        supervisor ?? throw new ArgumentNullException(nameof(supervisor));

    public Task StartAsync(CancellationToken cancellationToken) => _supervisor.StartAsync(cancellationToken);

    public Task StopAsync(CancellationToken cancellationToken) => _supervisor.StopAsync(cancellationToken);
}

/// <summary>
/// Placeholder for a host that wants to drive <see cref="BackendSupervisor"/>
/// itself. Registering nothing would be simpler, but the DI container needs a
/// concrete answer for the descriptor.
/// </summary>
internal sealed class NullHostedService : IHostedService
{
    public static NullHostedService Instance { get; } = new();

    public Task StartAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
}
