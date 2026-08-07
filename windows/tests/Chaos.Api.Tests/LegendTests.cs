using Chaos.Api.Annunciator;

namespace Chaos.Api.Tests;

/// <summary>
/// The engraved legends, and the fallback generator for definitions added later.
/// </summary>
public sealed class LegendTests
{
    [Fact]
    public void HandCutLegendIsUsedWhenOneExists() =>
        Assert.Equal(
            ["SOURCE XFER", "FAILED"],
            Legends.Engrave("Source transfer did not complete", "transfer_failed"));

    [Fact]
    public void EveryHandCutLegendFitsTheWindow()
    {
        foreach (var (key, lines) in Legends.HandCut)
        {
            Assert.InRange(lines.Count, 1, Legends.MaxLines);
            foreach (var line in lines)
            {
                Assert.True(line.Length <= Legends.Width, $"{key}: '{line}' is {line.Length} chars");
                Assert.Equal(line.ToUpperInvariant(), line);
            }
        }
    }

    [Fact]
    public void AllFortyShippedAlarmsHaveAHandCutLegend() =>
        Assert.Equal(40, Legends.HandCut.Count);

    [Theory]
    [InlineData("Source transfer did not complete", "COMPLETE")]
    [InlineData("Power container fluid detected", "DETECTED")]
    [InlineData("Some entirely new condition has failed", "FAILED")]
    public void GeneratedFallbackKeepsTheOperativeWord(string name, string mustContain)
    {
        // The meaning of an alarm name usually sits in its last word. Dropping
        // it produces legends like "SOURCE TRANSFER DID NOT", which is not a
        // shorter legend but a wrong one.
        var joined = string.Join(' ', Legends.Engrave(name));
        Assert.Contains(mustContain, joined, StringComparison.Ordinal);
    }

    [Fact]
    public void GeneratedFallbackRespectsTheWindow()
    {
        var lines = Legends.Engrave("Power container cooling failed and temperature is rising fast");

        Assert.True(lines.Count <= Legends.MaxLines);
        foreach (var line in lines)
        {
            Assert.True(line.Length <= Legends.Width, $"'{line}' is {line.Length} chars");
            Assert.False(line.EndsWith('-'));
        }
    }

    [Fact]
    public void EmptyNameProducesAnExplicitPlaceholder() =>
        Assert.Equal(["(UNNAMED)"], Legends.Engrave(string.Empty));

    [Fact]
    public void NullNameProducesAnExplicitPlaceholder() =>
        Assert.Equal(["(UNNAMED)"], Legends.Engrave(null));

    [Fact]
    public void PunctuationBecomesWhitespaceRatherThanBeingEngraved() =>
        Assert.DoesNotContain(
            ".",
            string.Join(' ', Legends.Engrave("Battery bank #1: state of charge low.")),
            StringComparison.Ordinal);

    [Fact]
    public void AbbreviationsAreAppliedBeforeWrapping()
    {
        // "STATE OF CHARGE" -> "SOC" is a phrase substitution, so it has to run
        // before stopword removal splits "of" out of the middle of it.
        var joined = string.Join(' ', Legends.Engrave("Battery state of charge low"));
        Assert.Contains("SOC", joined, StringComparison.Ordinal);
        Assert.Contains("BATT", joined, StringComparison.Ordinal);
    }

    [Fact]
    public void AWordLongerThanTheWindowGetsItsOwnLineRatherThanBeingTruncated()
    {
        var lines = Legends.Engrave("Supercalifragilistic failed");

        Assert.Contains("FAILED", lines);
        Assert.Contains(lines, line => line.Length > Legends.Width);
    }

    [Fact]
    public void AnAllStopwordNameStillProducesALegend()
    {
        // kept would be empty, so the generator falls back to the raw words
        // rather than returning nothing at all.
        var lines = Legends.Engrave("the of and");

        Assert.NotEmpty(lines);
        Assert.DoesNotContain("(UNNAMED)", lines);
    }
}
