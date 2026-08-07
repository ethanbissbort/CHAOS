using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

public sealed class CommandLineTokenizerTests
{
    [Fact]
    public void Splits_a_plain_command_line()
    {
        Assert.Equal(
            new[] { "--annunciator", "--monitor", "2" },
            CommandLineTokenizer.Split("--annunciator --monitor 2"));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void Empty_input_gives_no_arguments(string? input)
    {
        Assert.Empty(CommandLineTokenizer.Split(input));
    }

    [Fact]
    public void Collapses_runs_of_whitespace()
    {
        Assert.Equal(
            new[] { "a", "b" },
            CommandLineTokenizer.Split("  a \t  b  "));
    }

    [Fact]
    public void Quoted_arguments_keep_their_spaces()
    {
        Assert.Equal(
            new[] { "--host", "http://a b/", "--fullscreen" },
            CommandLineTokenizer.Split("--host \"http://a b/\" --fullscreen"));
    }

    [Fact]
    public void An_empty_quoted_argument_is_preserved()
    {
        Assert.Equal(new[] { "--host", "" }, CommandLineTokenizer.Split("--host \"\""));
    }

    [Fact]
    public void Backslashes_before_a_quote_follow_the_windows_rules()
    {
        // The case that breaks naive splitters: a quoted path ending in a
        // backslash, which is what Program Files paths look like.
        Assert.Equal(
            new[] { @"C:\Program Files\CHAOS\", "--annunciator" },
            CommandLineTokenizer.Split(@"""C:\Program Files\CHAOS\\"" --annunciator"));
    }

    [Fact]
    public void Backslashes_not_before_a_quote_are_literal()
    {
        Assert.Equal(
            new[] { @"C:\Program Files\CHAOS\shell.exe" },
            CommandLineTokenizer.Split(@"""C:\Program Files\CHAOS\shell.exe"""));
    }

    [Fact]
    public void An_escaped_quote_is_kept_in_the_value()
    {
        Assert.Equal(new[] { @"say ""hi""" }, CommandLineTokenizer.Split(@"""say \""hi\"""""));
    }

    [Fact]
    public void Unterminated_quotes_do_not_lose_the_last_argument()
    {
        Assert.Equal(new[] { "--host", "http://node" }, CommandLineTokenizer.Split("--host \"http://node"));
    }

    // ------------------------------------------------------------------------
    // Activation arguments, where a leading executable path may or may not be
    // present depending on how the second instance was started.
    // ------------------------------------------------------------------------

    [Fact]
    public void A_leading_executable_path_is_dropped()
    {
        var args = CommandLineTokenizer.SplitActivationArguments(
            @"""C:\Program Files\CHAOS\Chaos.Shell.exe"" --annunciator --monitor 2");

        Assert.Equal(new[] { "--annunciator", "--monitor", "2" }, args);
    }

    [Fact]
    public void An_unquoted_executable_path_is_dropped()
    {
        var args = CommandLineTokenizer.SplitActivationArguments(
            @"C:\CHAOS\Chaos.Shell.exe --acknowledge");

        Assert.Equal(new[] { "--acknowledge" }, args);
    }

    [Fact]
    public void A_command_line_that_is_already_argv_is_left_alone()
    {
        var args = CommandLineTokenizer.SplitActivationArguments("--annunciator --fullscreen");

        Assert.Equal(new[] { "--annunciator", "--fullscreen" }, args);
    }

    [Fact]
    public void Activation_round_trips_into_a_usable_payload()
    {
        // End to end: a wall-display shortcut launched while the shell is
        // already running must produce the same request either way.
        const string commandLine =
            @"""C:\Program Files\CHAOS\Chaos.Shell.exe"" --annunciator --monitor 2 --fullscreen";

        var args = CommandLineTokenizer.SplitActivationArguments(commandLine);
        var options = ShellCommandLine.Parse(args.ToArray());

        Assert.Equal(ActivationTarget.Annunciator, options.Activation.Target);
        Assert.Equal("2", options.Activation.Monitor);
        Assert.True(options.Activation.FullScreen);
        Assert.Empty(options.Problems);
    }

    [Fact]
    public void An_empty_activation_command_line_focuses_the_console()
    {
        var options = ShellCommandLine.Parse(
            CommandLineTokenizer.SplitActivationArguments("").ToArray());

        Assert.Equal(ActivationTarget.Console, options.Activation.Target);
    }
}
