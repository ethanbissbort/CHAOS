using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;
using Chaos.Api.Data;

namespace Chaos.Api.Http;

/// <summary>
/// JSON settings chosen to produce the same document the Python platform does.
/// </summary>
/// <remarks>
/// Existing clients — the browser console, the annunciator panel page, the WinUI
/// shell, Grafana — were written against FastAPI's output. A ported endpoint
/// that "improves" the encoding breaks them, so the differences that are
/// observable to a client are matched rather than modernised.
/// </remarks>
public static class PlatformJson
{
    /// <summary>Serializer options matching Starlette's <c>JSONResponse</c>.</summary>
    public static JsonSerializerOptions Options { get; } = Create();

    private static JsonSerializerOptions Create()
    {
        var options = new JsonSerializerOptions
        {
            // Starlette dumps with ensure_ascii=False and no indentation, so
            // non-ASCII and characters like ' are emitted raw. System.Text.Json
            // escapes them by default. The documents are identical once parsed,
            // but matching the bytes keeps diffs between the two
            // implementations readable during the migration.
            Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
            WriteIndented = false,

            // Property names are spelled explicitly with [JsonPropertyName] on
            // every DTO. No naming policy, so a renamed C# property cannot
            // silently rename a wire field.
            PropertyNamingPolicy = null,

            // Python emits nulls; every one of them is meaningful here (a tile
            // with no alarm has a null alarm_id, not a missing one).
            DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        };

        options.Converters.Add(new PlatformTimestampConverter());
        return options;
    }
}

/// <summary>
/// Writes <see cref="PlatformTimestamp"/> the way the Python endpoint writes it.
/// </summary>
/// <remarks>
/// See <see cref="PlatformTimestamp"/> for why the offset-known flag is carried
/// this far: on SQLite the platform emits naive ISO-8601 with no suffix, and a
/// browser parses that as local time. Matching it is not pedantry.
/// </remarks>
internal sealed class PlatformTimestampConverter : JsonConverter<PlatformTimestamp>
{
    public override PlatformTimestamp Read(
        ref Utf8JsonReader reader,
        Type typeToConvert,
        JsonSerializerOptions options)
    {
        var text = reader.GetString();
        return text is null
            ? throw new JsonException("Expected an ISO-8601 timestamp, found null.")
            : DbValues.ParseTimestampText(text);
    }

    public override void Write(
        Utf8JsonWriter writer,
        PlatformTimestamp value,
        JsonSerializerOptions options)
    {
        ArgumentNullException.ThrowIfNull(writer);
        writer.WriteStringValue(value.ToPlatformString());
    }
}
