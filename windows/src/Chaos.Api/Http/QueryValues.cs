using System.Text.Json.Serialization;

namespace Chaos.Api.Http;

/// <summary>
/// Query-string parsing that matches FastAPI's, including how it rejects.
/// </summary>
/// <remarks>
/// <para>
/// ASP.NET Core's own boolean binding accepts <c>true</c>/<c>false</c> and
/// nothing else, and fails with a 400. FastAPI (pydantic v2) accepts
/// <c>true/false/1/0/yes/no/on/off</c> case-insensitively and fails with a 422
/// carrying a structured <c>detail</c> array.
/// </para>
/// <para>
/// Every one of those differences is visible to a client, and the ported route
/// is reached through the same gateway URL as the unported ones. A caller that
/// sends <c>?include_pending=1</c> — which the platform has always accepted —
/// must not start getting a 400 because the route moved. So the divergence is
/// closed here rather than declared acceptable.
/// </para>
/// </remarks>
public static class QueryValues
{
    private static readonly HashSet<string> TrueValues =
        new(StringComparer.OrdinalIgnoreCase) { "true", "1", "yes", "on", "y", "t" };

    private static readonly HashSet<string> FalseValues =
        new(StringComparer.OrdinalIgnoreCase) { "false", "0", "no", "off", "n", "f" };

    /// <summary>
    /// Parses a boolean query value the way pydantic v2 does.
    /// </summary>
    /// <param name="raw">The raw query value, or null when the parameter is absent.</param>
    /// <param name="defaultValue">Value to use when the parameter is absent.</param>
    /// <param name="value">The parsed value.</param>
    /// <returns>False when the value is present but not interpretable.</returns>
    public static bool TryParseBool(string? raw, bool defaultValue, out bool value)
    {
        if (raw is null)
        {
            value = defaultValue;
            return true;
        }

        // Note: pydantic does NOT trim. An empty or whitespace value is a
        // validation error, matching what the Python platform returns today.
        if (TrueValues.Contains(raw))
        {
            value = true;
            return true;
        }

        if (FalseValues.Contains(raw))
        {
            value = false;
            return true;
        }

        value = defaultValue;
        return false;
    }
}

/// <summary>
/// One entry of FastAPI's 422 <c>detail</c> array.
/// </summary>
/// <param name="Type">pydantic error type, e.g. <c>bool_parsing</c>.</param>
/// <param name="Loc">Where the bad value was, e.g. <c>["query", "include_pending"]</c>.</param>
/// <param name="Msg">Human-readable message.</param>
/// <param name="Input">The value that failed to parse.</param>
public sealed record ValidationErrorDetail(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("loc")] IReadOnlyList<string> Loc,
    [property: JsonPropertyName("msg")] string Msg,
    [property: JsonPropertyName("input")] string Input);

/// <summary>FastAPI's 422 response body.</summary>
/// <param name="Detail">One entry per failed field.</param>
public sealed record ValidationErrorResponse(
    [property: JsonPropertyName("detail")] IReadOnlyList<ValidationErrorDetail> Detail)
{
    /// <summary>Builds the exact body pydantic produces for an unparseable boolean.</summary>
    public static ValidationErrorResponse BoolParsing(string parameterName, string input) =>
        new([
            new ValidationErrorDetail(
                Type: "bool_parsing",
                Loc: ["query", parameterName],
                Msg: "Input should be a valid boolean, unable to interpret input",
                Input: input),
        ]);
}
