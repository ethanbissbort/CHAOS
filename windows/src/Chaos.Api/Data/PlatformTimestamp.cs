using System.Globalization;

namespace Chaos.Api.Data;

/// <summary>
/// A platform timestamp together with whether the store knew its UTC offset.
/// </summary>
/// <remarks>
/// <para>
/// This distinction is not pedantry, it is the wire format. The Python platform
/// declares its timestamp columns <c>DateTime(timezone=True)</c>, but what comes
/// back depends on the backend:
/// </para>
/// <list type="bullet">
///   <item>
///     SQLite stores <c>'YYYY-MM-DD HH:MM:SS.ffffff'</c> as TEXT with no offset,
///     so SQLAlchemy hands FastAPI a naive datetime and the JSON reads
///     <c>2026-08-07T16:31:07.935433</c> — no <c>Z</c>.
///   </item>
///   <item>
///     PostgreSQL <c>timestamptz</c> round-trips the offset, so the same field
///     serialises as <c>2026-08-07T16:31:07.935433Z</c>.
///   </item>
/// </list>
/// <para>
/// A browser reading the first with <c>new Date(...)</c> interprets it as LOCAL
/// time; the second as UTC. On a site running <c>America/Toronto</c> that is a
/// four- or five-hour error on an alarm timestamp, so the difference is
/// operationally real and the port must reproduce it rather than "improve" it.
/// Carrying the offset-known flag from the reader is how this type does that.
/// </para>
/// <para>
/// Values are rendered with microsecond precision and the fraction omitted
/// entirely when it is zero, which is what Python's <c>datetime.isoformat()</c>
/// does and therefore what every existing client already parses.
/// </para>
/// </remarks>
public readonly record struct PlatformTimestamp
{
    /// <summary>Ticks per microsecond — .NET keeps 100 ns, Python keeps 1 µs.</summary>
    private const long TicksPerMicrosecond = 10L;

    private PlatformTimestamp(DateTime value, TimeSpan? offset)
    {
        // Truncate to microseconds. Python datetimes have no finer resolution,
        // so a .NET value carrying 100 ns ticks (DateTime.UtcNow does) would
        // render a fraction where Python renders none, or a seventh digit that
        // no existing client has ever seen.
        Value = new DateTime(
            value.Ticks - (value.Ticks % TicksPerMicrosecond),
            value.Kind);
        Offset = offset;
    }

    /// <summary>The instant, as stored. Never adjusted.</summary>
    public DateTime Value { get; }

    /// <summary>
    /// The UTC offset when the store knew one, otherwise <see langword="null"/>
    /// for a naive value that must serialise without a suffix.
    /// </summary>
    public TimeSpan? Offset { get; }

    /// <summary>True when this value serialises with an explicit offset.</summary>
    public bool HasOffset => Offset.HasValue;

    /// <summary>A value whose offset is unknown — serialises with no suffix.</summary>
    public static PlatformTimestamp Naive(DateTime value) =>
        new(DateTime.SpecifyKind(value, DateTimeKind.Unspecified), null);

    /// <summary>A UTC value — serialises with a trailing <c>Z</c>.</summary>
    public static PlatformTimestamp Utc(DateTime value) =>
        new(DateTime.SpecifyKind(value, DateTimeKind.Utc), TimeSpan.Zero);

    /// <summary>A value at a known non-UTC offset.</summary>
    public static PlatformTimestamp AtOffset(DateTime value, TimeSpan offset) =>
        new(DateTime.SpecifyKind(value, DateTimeKind.Unspecified), offset);

    /// <summary>
    /// Renders exactly as the Python platform renders the same value.
    /// </summary>
    /// <remarks>
    /// FastAPI serialises this endpoint through pydantic (the handler carries a
    /// <c>-> dict[str, Any]</c> return annotation), which renders a zero UTC
    /// offset as <c>Z</c> rather than <c>+00:00</c>. Handlers without that
    /// annotation go through <c>jsonable_encoder</c> and emit <c>+00:00</c>
    /// instead. Both are ISO-8601; they are not the same bytes. Anything ported
    /// out of this codebase has to check which serialiser its handler used.
    /// </remarks>
    public string ToPlatformString()
    {
        var text = Value.ToString(
            Value.Ticks % TimeSpan.TicksPerSecond == 0
                ? "yyyy-MM-ddTHH:mm:ss"
                : "yyyy-MM-ddTHH:mm:ss.ffffff",
            CultureInfo.InvariantCulture);

        if (Offset is not { } offset)
        {
            return text;
        }

        if (offset == TimeSpan.Zero)
        {
            return text + "Z";
        }

        var sign = offset < TimeSpan.Zero ? '-' : '+';
        var absolute = offset.Duration();
        return string.Create(
            CultureInfo.InvariantCulture,
            $"{text}{sign}{absolute.Hours:D2}:{absolute.Minutes:D2}");
    }

    /// <inheritdoc />
    public override string ToString() => ToPlatformString();
}
