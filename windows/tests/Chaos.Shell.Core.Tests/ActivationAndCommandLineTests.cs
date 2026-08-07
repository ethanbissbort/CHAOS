using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>
/// Single-instance activation: what a second launch asks the running instance
/// to do, and how a launch that cannot be understood is handled.
/// </summary>
public sealed class ActivationPayloadTests
{
    [Fact]
    public void Round_trips_through_json()
    {
        var payload = new ActivationPayload
        {
            Target = ActivationTarget.Annunciator,
            Monitor = "2",
            FullScreen = true,
            AlwaysOnTop = true,
        };

        Assert.True(ActivationPayload.TryParse(payload.Serialize(), out var parsed, out var problem));

        Assert.Null(problem);
        Assert.Equal(payload, parsed);
    }

    [Fact]
    public void Round_trips_through_command_line_arguments()
    {
        // This is the real transport: WinUI redirects a launch to the running
        // instance as a command line, which is re-parsed there.
        var payload = new ActivationPayload
        {
            Target = ActivationTarget.Acknowledge,
            Monitor = "primary",
            FullScreen = true,
            AlwaysOnTop = true,
        };

        var reparsed = ShellCommandLine.Parse(payload.ToArguments().ToArray()).Activation;

        Assert.Equal(payload, reparsed);
    }

    [Fact]
    public void Default_activation_round_trips_to_an_empty_argument_list()
    {
        var payload = ActivationPayload.FocusConsole;

        Assert.Empty(payload.ToArguments());
        Assert.Equal(payload, ShellCommandLine.Parse(payload.ToArguments().ToArray()).Activation);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not json at all")]
    [InlineData("{ broken")]
    [InlineData("[]")]
    [InlineData("null")]
    public void Unreadable_activation_falls_back_to_focusing_the_console(string? text)
    {
        // A second launch must never be able to take down the running shell,
        // whatever it sends down the pipe.
        var payload = ActivationPayload.ParseOrFocusConsole(text);

        Assert.Equal(ActivationTarget.Console, payload.Target);
        Assert.Equal(ActivationPayload.FocusConsole, payload);

        Assert.False(ActivationPayload.TryParse(text, out _, out var problem));
        Assert.NotNull(problem);
    }

    [Fact]
    public void Payload_from_a_future_protocol_version_is_refused_not_guessed()
    {
        // A newer build installed while the old process is still resident.
        var future = """{"v":99,"target":"Annunciator"}""";

        Assert.False(ActivationPayload.TryParse(future, out var payload, out var problem));

        Assert.Equal(ActivationPayload.FocusConsole, payload);
        Assert.Contains("v99", problem);
        Assert.Equal(ActivationTarget.Console, ActivationPayload.ParseOrFocusConsole(future).Target);
    }

    [Theory]
    [InlineData(ActivationTarget.Console, false)]
    [InlineData(ActivationTarget.Annunciator, true)]
    [InlineData(ActivationTarget.Acknowledge, true)]
    public void Acknowledge_opens_the_annunciator(ActivationTarget target, bool expected)
    {
        var payload = new ActivationPayload { Target = target };

        Assert.Equal(expected, payload.WantsAnnunciator);
    }
}

/// <summary>Command-line parsing, including everything a bad shortcut can contain.</summary>
public sealed class ShellCommandLineTests
{
    [Fact]
    public void No_arguments_gives_the_default()
    {
        var options = ShellCommandLine.Parse(Array.Empty<string>());

        Assert.Equal(ActivationTarget.Console, options.Activation.Target);
        Assert.Null(options.HostOverride);
        Assert.Empty(options.Problems);
    }

    [Fact]
    public void Null_arguments_are_tolerated()
    {
        var options = ShellCommandLine.Parse(null);

        Assert.Equal(ShellStartupOptions.Default, options);
    }

    [Theory]
    [InlineData("--annunciator")]
    [InlineData("-annunciator")]
    [InlineData("/annunciator")]
    [InlineData("--ANNUNCIATOR")]
    public void Annunciator_switch_accepts_the_usual_spellings(string arg)
    {
        var options = ShellCommandLine.Parse(new[] { arg });

        Assert.Equal(ActivationTarget.Annunciator, options.Activation.Target);
        Assert.Empty(options.Problems);
    }

    [Theory]
    [InlineData("--acknowledge")]
    [InlineData("--ack")]
    public void Acknowledge_switch(string arg)
    {
        var options = ShellCommandLine.Parse(new[] { arg });

        Assert.Equal(ActivationTarget.Acknowledge, options.Activation.Target);
    }

    [Fact]
    public void Wall_display_invocation_parses_completely()
    {
        var options = ShellCommandLine.Parse(
            new[] { "--annunciator", "--monitor", "2", "--fullscreen", "--always-on-top" });

        Assert.Equal(ActivationTarget.Annunciator, options.Activation.Target);
        Assert.Equal("2", options.Activation.Monitor);
        Assert.True(options.Activation.FullScreen);
        Assert.True(options.Activation.AlwaysOnTop);
        Assert.Empty(options.Problems);
    }

    [Theory]
    [InlineData(new[] { "--host", "http://node:8080" }, "http://node:8080")]
    [InlineData(new[] { "--host=http://node:8080" }, "http://node:8080")]
    public void Host_override_accepts_both_separated_and_inline_values(string[] args, string expected)
    {
        var options = ShellCommandLine.Parse(args);

        Assert.Equal(expected, options.HostOverride);
        Assert.Empty(options.Problems);
    }

    [Fact]
    public void Missing_value_is_reported_but_does_not_stop_the_launch()
    {
        var options = ShellCommandLine.Parse(new[] { "--host" });

        Assert.Null(options.HostOverride);
        Assert.Contains(options.Problems, p => p.Contains("--host", StringComparison.Ordinal));

        // Still a usable launch: the shell opens on the default gateway.
        Assert.Equal(ActivationTarget.Console, options.Activation.Target);
    }

    [Fact]
    public void Unknown_argument_is_reported_rather_than_fatal()
    {
        var options = ShellCommandLine.Parse(new[] { "--wat", "--annunciator" });

        Assert.Single(options.Problems);
        Assert.Contains("--wat", options.Problems[0], StringComparison.Ordinal);
        Assert.Equal(ActivationTarget.Annunciator, options.Activation.Target);
    }

    [Fact]
    public void Display_flags_without_annunciator_are_flagged_as_meaningless()
    {
        var options = ShellCommandLine.Parse(new[] { "--monitor", "2", "--fullscreen" });

        Assert.NotEmpty(options.Problems);
        Assert.Contains(options.Problems, p => p.Contains("--annunciator", StringComparison.Ordinal));
    }

    [Fact]
    public void Empty_and_whitespace_arguments_are_skipped()
    {
        var options = ShellCommandLine.Parse(new[] { "", "   ", "--annunciator" });

        Assert.Equal(ActivationTarget.Annunciator, options.Activation.Target);
        Assert.Empty(options.Problems);
    }

    [Fact]
    public void A_switch_is_not_swallowed_as_the_value_of_a_previous_switch()
    {
        var options = ShellCommandLine.Parse(new[] { "--monitor", "--annunciator" });

        Assert.Equal(ActivationTarget.Annunciator, options.Activation.Target);
        Assert.Null(options.Activation.Monitor);
        Assert.Contains(options.Problems, p => p.Contains("--monitor", StringComparison.Ordinal));
    }
}
