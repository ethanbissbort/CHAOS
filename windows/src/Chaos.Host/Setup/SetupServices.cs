extern alias ChaosSupervisor;

using Chaos.Host.Configuration;
using ChaosSupervisor::Chaos.Host.Supervisor.Processes;
using ChaosSupervisor::Chaos.Host.Supervisor.Runtime;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Setup;

/// <summary>Registers first-run setup.</summary>
/// <remarks>
/// Every collaborator goes in with <c>TryAdd</c>, and they are the same types
/// <c>AddChaosBackendSupervisor</c> registers. Either call order gives one
/// interpreter resolver and one process launcher shared by setup and the
/// backend, which is the point: the two must never disagree about which Python
/// runs the platform.
/// </remarks>
internal static class SetupServices
{
    /// <summary>Adds the setup coordinator, its collaborators and its startup hook.</summary>
    /// <param name="services">The service collection.</param>
    /// <returns>The collection, for chaining.</returns>
    public static IServiceCollection AddPlatformSetup(this IServiceCollection services)
    {
        ArgumentNullException.ThrowIfNull(services);

        services.TryAddSingleton<IFileSystem>(PhysicalFileSystem.Instance);
        services.TryAddSingleton<IPythonRuntimeResolver>(
            provider => new PythonRuntimeResolver(provider.GetRequiredService<IFileSystem>()));
        services.TryAddSingleton<IProcessLauncher>(
            provider => new SystemProcessLauncher(provider.GetRequiredService<ILogger<SystemProcessLauncher>>()));

        services.TryAddSingleton<ISetupLog>(provider => SetupLogFactory.Create(
            provider.GetRequiredService<IOptions<ChaosHostOptions>>().Value));

        services.TryAddSingleton<IPlatformCommandRunner, PlatformCommandRunner>();
        services.TryAddSingleton<PlatformSetupCoordinator>();
        services.AddHostedService<SetupHostedService>();

        return services;
    }
}
