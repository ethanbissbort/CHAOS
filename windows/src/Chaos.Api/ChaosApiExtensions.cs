using Chaos.Api.Annunciator;
using Chaos.Api.Data;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace Chaos.Api;

/// <summary>
/// Registration for the natively-served slice of <c>/api/v1</c>.
/// </summary>
/// <remarks>
/// Two calls, deliberately: <see cref="AddChaosApi(IServiceCollection, Action{ChaosApiOptions})"/>
/// in the service phase and <see cref="MapChaosApi(IEndpointRouteBuilder)"/> in
/// the routing phase. <c>Chaos.Host</c> owns composition — the listener,
/// configuration binding, the reverse proxy and the route-ownership manifest —
/// and this assembly contributes endpoints to it and nothing else.
/// </remarks>
public static class ChaosApiExtensions
{
    /// <summary>
    /// Registers the services the ported endpoints need.
    /// </summary>
    /// <param name="services">The host's service collection.</param>
    /// <param name="configure">Configures <see cref="ChaosApiOptions"/>.</param>
    /// <returns>The same collection, for chaining.</returns>
    /// <remarks>
    /// Registrations use TryAdd, so a host that wants PostgreSQL registers its
    /// own <see cref="IChaosConnectionFactory"/> BEFORE calling this and wins.
    /// That is the whole extension point for the deployment backend.
    /// </remarks>
    public static IServiceCollection AddChaosApi(
        this IServiceCollection services,
        Action<ChaosApiOptions> configure)
    {
        ArgumentNullException.ThrowIfNull(services);
        ArgumentNullException.ThrowIfNull(configure);

        services.Configure(configure);
        return services.AddChaosApi();
    }

    /// <summary>
    /// Registers the services the ported endpoints need, taking
    /// <see cref="ChaosApiOptions"/> from configuration already bound by the host.
    /// </summary>
    /// <param name="services">The host's service collection.</param>
    /// <returns>The same collection, for chaining.</returns>
    public static IServiceCollection AddChaosApi(this IServiceCollection services)
    {
        ArgumentNullException.ThrowIfNull(services);

        services.AddOptions<ChaosApiOptions>();
        services.TryAddSingleton(TimeProvider.System);

        services.TryAddSingleton<IChaosConnectionFactory>(provider =>
        {
            var options = provider.GetRequiredService<
                Microsoft.Extensions.Options.IOptions<ChaosApiOptions>>().Value;

            if (string.IsNullOrWhiteSpace(options.SqliteDatabasePath))
            {
                throw new InvalidOperationException(
                    "Chaos.Api has no database to read. Set ChaosApiOptions.SqliteDatabasePath, "
                    + "or register an IChaosConnectionFactory before calling AddChaosApi() to use "
                    + "PostgreSQL. It must open the database READ-ONLY: the Python platform owns "
                    + "this schema.");
            }

            return new SqliteReadOnlyConnectionFactory(options.SqliteDatabasePath);
        });

        services.TryAddScoped<IAnnunciatorReadStore, AnnunciatorReadStore>();

        return services;
    }

    /// <summary>
    /// Maps every route this assembly serves natively.
    /// </summary>
    /// <param name="endpoints">The host's endpoint route builder.</param>
    /// <returns>The same builder, for chaining.</returns>
    /// <remarks>
    /// Routes are mapped at their absolute platform paths. The gateway must have
    /// the matching prefixes marked <c>Dotnet</c> in its route-ownership
    /// manifest, or the reverse proxy will forward them to Python before they
    /// reach here. <see cref="ChaosApiRoutes.OwnedPrefixes"/> is the list to
    /// check that against.
    /// </remarks>
    public static IEndpointRouteBuilder MapChaosApi(this IEndpointRouteBuilder endpoints)
    {
        ArgumentNullException.ThrowIfNull(endpoints);

        endpoints.MapGet(ChaosApiRoutes.Annunciator, AnnunciatorEndpoint.HandleAsync)
            .WithName("ChaosAnnunciatorPanel")
            .WithTags("alarms")
            .WithSummary("Annunciator panel state (SDD 14, 17.3)");

        return endpoints;
    }
}
