using System.Data.Common;
using System.Globalization;

namespace Chaos.Api.Data;

/// <summary>
/// Provider-tolerant readers for the handful of column shapes this API reads.
/// </summary>
/// <remarks>
/// SQLite and PostgreSQL disagree about the CLR type behind the same SQLAlchemy
/// column: <c>Boolean</c> arrives as <see cref="long"/> from SQLite and
/// <see cref="bool"/> from Npgsql; <c>DateTime(timezone=True)</c> arrives as
/// TEXT from SQLite and <see cref="DateTime"/> with <see cref="DateTimeKind.Utc"/>
/// from Npgsql. Reading through these helpers is what keeps one set of queries
/// working against both, and keeps the offset-known distinction that
/// <see cref="PlatformTimestamp"/> exists to carry.
/// </remarks>
internal static class DbValues
{
    internal static string GetString(DbDataReader reader, int ordinal) =>
        GetStringOrNull(reader, ordinal)
        ?? throw new ChaosDataException(
            $"Column '{reader.GetName(ordinal)}' is NULL but the platform schema declares it NOT NULL.");

    internal static string? GetStringOrNull(DbDataReader reader, int ordinal) =>
        reader.IsDBNull(ordinal) ? null : Convert.ToString(reader.GetValue(ordinal), CultureInfo.InvariantCulture);

    /// <summary>
    /// Reads a SQLAlchemy <c>Boolean</c> column.
    /// </summary>
    /// <remarks>
    /// NULL reads as <see langword="false"/>. That matches SQLAlchemy's own
    /// behaviour for these columns, which carry <c>default=True/False</c> rather
    /// than a NOT NULL constraint, so a row written before the column existed
    /// reads as the falsy value on both sides. For <c>enabled</c> that fails
    /// safe: a definition with no answer is treated as not enabled, and the tile
    /// reports out of service rather than dark.
    /// </remarks>
    internal static bool GetBoolean(DbDataReader reader, int ordinal)
    {
        if (reader.IsDBNull(ordinal))
        {
            return false;
        }

        var value = reader.GetValue(ordinal);
        return value switch
        {
            bool b => b,
            long l => l != 0,
            int i => i != 0,
            short s => s != 0,
            byte b => b != 0,
            string s => bool.TryParse(s, out var parsed)
                ? parsed
                : s is "1" or "t" or "true" or "TRUE",
            _ => Convert.ToBoolean(value, CultureInfo.InvariantCulture),
        };
    }

    /// <summary>
    /// Reads a timestamp column, preserving whether the store knew its offset.
    /// </summary>
    internal static PlatformTimestamp? GetTimestamp(DbDataReader reader, int ordinal)
    {
        if (reader.IsDBNull(ordinal))
        {
            return null;
        }

        var value = reader.GetValue(ordinal);
        return value switch
        {
            DateTimeOffset dto => dto.Offset == TimeSpan.Zero
                ? PlatformTimestamp.Utc(dto.UtcDateTime)
                : PlatformTimestamp.AtOffset(dto.DateTime, dto.Offset),
            DateTime dt => dt.Kind switch
            {
                DateTimeKind.Utc => PlatformTimestamp.Utc(dt),
                DateTimeKind.Local => PlatformTimestamp.AtOffset(dt, TimeZoneInfo.Local.GetUtcOffset(dt)),
                _ => PlatformTimestamp.Naive(dt),
            },
            string text => ParseTimestampText(text),
            _ => throw new ChaosDataException(
                $"Column '{reader.GetName(ordinal)}' returned {value.GetType().Name}, which is not a timestamp."),
        };
    }

    /// <summary>
    /// Parses the text SQLAlchemy's SQLite dialect writes, and any ISO-8601
    /// variant a different writer might have left behind.
    /// </summary>
    internal static PlatformTimestamp ParseTimestampText(string text)
    {
        var trimmed = text.Trim();
        if (trimmed.Length == 0)
        {
            throw new ChaosDataException("Empty string where a timestamp was expected.");
        }

        if (HasExplicitOffset(trimmed))
        {
            if (!DateTimeOffset.TryParse(
                    trimmed,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.None,
                    out var withOffset))
            {
                throw new ChaosDataException($"Could not parse '{text}' as a timestamp with an offset.");
            }

            return withOffset.Offset == TimeSpan.Zero
                ? PlatformTimestamp.Utc(withOffset.UtcDateTime)
                : PlatformTimestamp.AtOffset(withOffset.DateTime, withOffset.Offset);
        }

        if (!DateTime.TryParse(
                trimmed,
                CultureInfo.InvariantCulture,
                DateTimeStyles.NoCurrentDateDefault,
                out var naive))
        {
            throw new ChaosDataException($"Could not parse '{text}' as a timestamp.");
        }

        return PlatformTimestamp.Naive(naive);
    }

    /// <summary>
    /// True when the text carries a UTC designator or a numeric offset.
    /// </summary>
    /// <remarks>
    /// Scanning from the end of the TIME portion is what makes this safe: the
    /// date already contains '-' separators, so a naive
    /// <c>2026-08-07 16:31:07.935433</c> must not be mistaken for an offset.
    /// </remarks>
    private static bool HasExplicitOffset(string text)
    {
        var timeStart = text.IndexOf(':');
        if (timeStart < 0)
        {
            return text.EndsWith('Z') || text.EndsWith('z');
        }

        var tail = text.AsSpan(timeStart);
        return tail.IndexOf('Z') >= 0
            || tail.IndexOf('z') >= 0
            || tail.IndexOf('+') >= 0
            || tail.IndexOf('-') >= 0;
    }
}

/// <summary>
/// The platform database did not look the way this API expects.
/// </summary>
/// <remarks>
/// Raised loudly and specifically rather than being papered over. Python owns
/// the schema, so drift here means the two implementations have diverged and a
/// 500 naming the column is far more useful than a silently wrong panel.
/// </remarks>
public sealed class ChaosDataException : Exception
{
    /// <summary>Creates the exception with a message describing the drift.</summary>
    public ChaosDataException(string message)
        : base(message)
    {
    }

    /// <summary>Creates the exception wrapping an underlying provider failure.</summary>
    public ChaosDataException(string message, Exception inner)
        : base(message, inner)
    {
    }
}
