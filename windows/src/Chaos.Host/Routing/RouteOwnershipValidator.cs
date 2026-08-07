using System.Text;

namespace Chaos.Host.Routing;

/// <summary>
/// Checks that every prefix the manifest claims for .NET is actually served by
/// a registered .NET endpoint.
/// </summary>
/// <remarks>
/// <para>
/// This is the guard that makes the migration seam safe to use. Flipping a row
/// to <see cref="RouteOwner.Dotnet"/> stops the gateway proxying that prefix to
/// Python. If the .NET endpoint was never written, never registered, or was
/// registered at a slightly different path, every request under that prefix
/// becomes a 404 — a silent, plausible-looking failure that a control system can
/// carry for weeks. An alarm endpoint that returns 404 reads, to most clients,
/// like "no alarms".
/// </para>
/// <para>
/// So this throws and the host does not start. Loud beats quiet.
/// </para>
/// </remarks>
public static class RouteOwnershipValidator
{
    /// <summary>
    /// Validates the table against the endpoints this host registered.
    /// </summary>
    /// <param name="table">The built ownership table.</param>
    /// <param name="registeredPatterns">
    /// Route patterns of every registered endpoint, normally
    /// <see cref="NativeRouteInventory.Patterns"/>.
    /// </param>
    /// <exception cref="RouteOwnershipException">
    /// One or more <see cref="RouteOwner.Dotnet"/> rows have no endpoint beneath them.
    /// </exception>
    public static void Validate(RouteOwnershipTable table, IReadOnlyList<string> registeredPatterns)
    {
        ArgumentNullException.ThrowIfNull(table);
        ArgumentNullException.ThrowIfNull(registeredPatterns);

        var unbacked = table.Entries
            .Where(entry => entry.Owner == RouteOwner.Dotnet)
            .Where(entry => !registeredPatterns.Any(pattern => RouteOwnershipTable.IsUnderPrefix(pattern, entry.PathPrefix)))
            .ToList();

        if (unbacked.Count == 0)
        {
            return;
        }

        throw new RouteOwnershipException(BuildMessage(unbacked, registeredPatterns));
    }

    private static string BuildMessage(
        IReadOnlyList<RouteOwnershipEntry> unbacked,
        IReadOnlyList<string> registeredPatterns)
    {
        var builder = new StringBuilder();
        builder.Append("Route ownership validation failed: ")
               .Append(unbacked.Count)
               .Append(unbacked.Count == 1 ? " route prefix is" : " route prefixes are")
               .AppendLine(" marked Dotnet but no .NET endpoint is registered beneath them.")
               .AppendLine();

        foreach (var entry in unbacked)
        {
            builder.Append("  ").Append(entry.PathPrefix)
                   .Append("   (source: ").Append(entry.Source);
            if (entry.PortedInVersion is { Length: > 0 } version)
            {
                builder.Append(", portedInVersion: ").Append(version);
            }

            builder.AppendLine(")");

            if (entry.Notes is { Length: > 0 } notes)
            {
                builder.Append("      notes: ").AppendLine(notes);
            }
        }

        builder.AppendLine()
               .Append("Registered .NET endpoint patterns (")
               .Append(registeredPatterns.Count)
               .AppendLine("):");

        if (registeredPatterns.Count == 0)
        {
            builder.AppendLine("  (none)");
        }
        else
        {
            foreach (var pattern in registeredPatterns)
            {
                builder.Append("  ").AppendLine(pattern);
            }
        }

        builder.AppendLine()
               .AppendLine("A Dotnet-owned prefix with nothing behind it returns 404 for a route the platform")
               .AppendLine("depends on, and a 404 during a migration is indistinguishable from an empty result.")
               .AppendLine("Either register the endpoint, or set Owner=Python for that prefix until it is ported.");

        return builder.ToString();
    }
}
