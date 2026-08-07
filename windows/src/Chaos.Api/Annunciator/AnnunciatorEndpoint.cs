using Chaos.Api.Data;
using Chaos.Api.Http;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;

namespace Chaos.Api.Annunciator;

/// <summary>
/// <c>GET /api/v1/annunciator</c> — the annunciator panel (SDD 14, 17.3).
/// </summary>
/// <remarks>
/// A hardwired annunciator panel is a fixed grid of engraved windows, one per
/// alarm condition, in a position that never moves. Operators learn the panel by
/// muscle memory: the tile in the third row of the energy bay <i>is</i> battery
/// reserve, lit or not. Every definition gets a tile, always, whether or not it
/// is currently in alarm.
/// </remarks>
public static class AnnunciatorEndpoint
{
    /// <summary>Name of the on-delay query parameter.</summary>
    internal const string IncludePendingParameter = "include_pending";

    /// <summary>
    /// Handles the request.
    /// </summary>
    /// <param name="context">The request, read for its query string.</param>
    /// <param name="store">Read-only access to the platform database.</param>
    /// <param name="timeProvider">Supplies <c>generated_at</c>.</param>
    /// <param name="logger">Diagnostics.</param>
    public static async Task<IResult> HandleAsync(
        HttpContext context,
        IAnnunciatorReadStore store,
        TimeProvider timeProvider,
        ILogger<AnnunciatorPanel> logger)
    {
        ArgumentNullException.ThrowIfNull(context);
        ArgumentNullException.ThrowIfNull(store);
        ArgumentNullException.ThrowIfNull(timeProvider);
        ArgumentNullException.ThrowIfNull(logger);

        // Starlette's QueryParams is a multidict whose scalar lookup returns the
        // LAST occurrence, so ?include_pending=true&include_pending=false is
        // false on the Python side. ASP.NET Core would join them into
        // "true,false" and reject; take the last value to match.
        var raw = context.Request.Query.TryGetValue(IncludePendingParameter, out var values) && values.Count > 0
            ? values[^1]
            : null;

        if (!QueryValues.TryParseBool(raw, defaultValue: false, out var includePending))
        {
            return Results.Json(
                ValidationErrorResponse.BoolParsing(IncludePendingParameter, raw ?? string.Empty),
                PlatformJson.Options,
                contentType: "application/json",
                statusCode: StatusCodes.Status422UnprocessableEntity);
        }

        AnnunciatorSnapshot snapshot;
        try
        {
            snapshot = await store.ReadAsync(includePending, context.RequestAborted).ConfigureAwait(false);
        }
        catch (ChaosDataException ex)
        {
            // Python owns this schema. Drift is a migration defect, not a
            // transient fault, so it is logged as an error and reported as one
            // rather than degraded into an empty panel — an empty panel would
            // read as "nothing wrong anywhere", which is the single most
            // dangerous thing this endpoint can say.
            logger.LogError(ex, "Annunciator panel could not be read from the platform database.");
            return Results.Problem(
                title: "The annunciator panel could not be read.",
                detail: ex.Message,
                statusCode: StatusCodes.Status500InternalServerError);
        }

        var panel = AnnunciatorPanelBuilder.Build(
            snapshot,
            PlatformTimestamp.Utc(timeProvider.GetUtcNow().UtcDateTime));

        // Content type is written without a charset because that is what
        // Starlette sends and what every existing client has parsed.
        return Results.Json(panel, PlatformJson.Options, contentType: "application/json");
    }
}
