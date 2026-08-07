using Chaos.Api.Data;

namespace Chaos.Api.Tests;

/// <summary>
/// Wire format for timestamps, pinned against what FastAPI actually emits.
/// </summary>
/// <remarks>
/// These expectations were taken by observation from the running Python
/// endpoint, not from the ISO-8601 standard. The platform's format is what its
/// clients already parse, and two of the rules here are surprising enough that
/// getting them from first principles would have produced the wrong answer.
/// </remarks>
public sealed class PlatformTimestampTests
{
    [Fact]
    public void MicrosecondsAreRenderedWithExactlySixDigits() =>
        Assert.Equal(
            "2026-01-02T03:04:05.123456",
            PlatformTimestamp.Naive(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Unspecified).AddTicks(1234560))
                .ToPlatformString());

    [Fact]
    public void TrailingZeroesInTheFractionAreKept() =>
        Assert.Equal(
            "2026-01-02T03:04:05.120000",
            PlatformTimestamp.Naive(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Unspecified).AddTicks(1200000))
                .ToPlatformString());

    [Fact]
    public void AZeroFractionIsOmittedEntirely() =>
        // Python's datetime.isoformat() drops the fraction when microsecond is
        // 0. A ".000000" here would be a value no client has ever seen.
        Assert.Equal(
            "2026-01-02T03:04:05",
            PlatformTimestamp.Naive(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Unspecified)).ToPlatformString());

    [Fact]
    public void SubMicrosecondTicksAreTruncatedNotRounded()
    {
        // DateTime.UtcNow carries 100 ns ticks; Python has no such resolution.
        // Rounding up could push the value into the next microsecond.
        var value = new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Utc).AddTicks(9);

        Assert.Equal("2026-01-02T03:04:05Z", PlatformTimestamp.Utc(value).ToPlatformString());
    }

    [Fact]
    public void UtcIsRenderedWithZNotPlusZeroZero() =>
        // This endpoint's handler carries a `-> dict[str, Any]` annotation, so
        // FastAPI serialises it through pydantic, which writes "Z". Handlers
        // without that annotation go through jsonable_encoder and write
        // "+00:00" instead. Same standard, different bytes.
        Assert.Equal(
            "2026-01-02T03:04:05.123456Z",
            PlatformTimestamp.Utc(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Utc).AddTicks(1234560))
                .ToPlatformString());

    [Fact]
    public void ANaiveValueGetsNoSuffixAtAll() =>
        // SQLite stores these columns as offset-free TEXT, so SQLAlchemy hands
        // FastAPI a naive datetime whatever timezone=True says. A browser
        // parses the result as LOCAL time. Reproduced deliberately.
        Assert.DoesNotContain(
            "Z",
            PlatformTimestamp.Naive(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Unspecified)).ToPlatformString(),
            StringComparison.Ordinal);

    [Fact]
    public void ANonZeroOffsetIsRenderedNumerically() =>
        Assert.Equal(
            "2026-01-02T03:04:05.123456-05:00",
            PlatformTimestamp.AtOffset(
                    new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Unspecified).AddTicks(1234560),
                    TimeSpan.FromHours(-5))
                .ToPlatformString());

    // ----------------------------------------------------------- parsing --

    [Fact]
    public void SqlAlchemysSqliteTextRoundTripsAsNaive()
    {
        var parsed = DbValues.ParseTimestampText("2026-08-07 16:31:07.935433");

        Assert.False(parsed.HasOffset);
        Assert.Equal("2026-08-07T16:31:07.935433", parsed.ToPlatformString());
    }

    [Fact]
    public void ADateWithHyphensIsNotMistakenForAnOffset()
    {
        // The date part is full of '-'. Offset detection scans from the time
        // separator so a naive value cannot be misread as offset-bearing.
        var parsed = DbValues.ParseTimestampText("2026-08-07 16:31:07");

        Assert.False(parsed.HasOffset);
    }

    [Fact]
    public void AnExplicitZIsCarriedThrough()
    {
        var parsed = DbValues.ParseTimestampText("2026-08-07T16:31:07.935433Z");

        Assert.True(parsed.HasOffset);
        Assert.Equal("2026-08-07T16:31:07.935433Z", parsed.ToPlatformString());
    }

    [Fact]
    public void AnExplicitNumericOffsetIsCarriedThrough()
    {
        var parsed = DbValues.ParseTimestampText("2026-08-07T16:31:07.935433-05:00");

        Assert.True(parsed.HasOffset);
        Assert.Equal("2026-08-07T16:31:07.935433-05:00", parsed.ToPlatformString());
    }

    [Fact]
    public void UnparseableTextIsRejectedRatherThanDefaulted() =>
        // A wrong-but-plausible timestamp on an alarm is worse than an error.
        Assert.Throws<ChaosDataException>(() => DbValues.ParseTimestampText("not a timestamp"));
}
