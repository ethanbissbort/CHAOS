using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

public sealed class LayoutStoreTests : IDisposable
{
    private readonly string _directory = Path.Combine(
        Path.GetTempPath(),
        "chaos-shell-tests-" + Guid.NewGuid().ToString("N"));

    private string LayoutPath => Path.Combine(_directory, "layout.json");

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }

    [Fact]
    public void Round_trips_a_layout()
    {
        var store = new FileLayoutStore(LayoutPath);

        var layout = new ShellLayout
        {
            Main = WindowPlacement.FromBounds(new ScreenRect(100, 50, 1400, 900), "0,0,1920x1080"),
            Annunciator = new WindowPlacement
            {
                Left = 1920,
                Top = 0,
                Width = 3840,
                Height = 2160,
                MonitorDeviceId = "1920,0,3840x2160",
                FullScreen = true,
                AlwaysOnTop = true,
                Zoom = 1.25,
            },
            AnnunciatorWasOpen = true,
            LastHost = "http://chaos-node:8080/",
        };

        Assert.Null(store.Save(layout));
        Assert.Equal(layout, store.Load());
    }

    [Fact]
    public void A_missing_file_loads_as_empty()
    {
        Assert.Equal(ShellLayout.Empty, new FileLayoutStore(LayoutPath).Load());
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("{ this is not json")]
    [InlineData("[]")]
    [InlineData("{\"v\": 99, \"main\": {}}")]
    public void A_corrupt_or_future_file_does_not_stop_the_shell_starting(string content)
    {
        // Losing a window position is a nuisance. An operator locked out of the
        // console by a bad preferences file is a fault.
        Directory.CreateDirectory(_directory);
        File.WriteAllText(LayoutPath, content);

        Assert.Equal(ShellLayout.Empty, new FileLayoutStore(LayoutPath).Load());
    }

    [Fact]
    public void Saving_creates_the_directory()
    {
        var store = new FileLayoutStore(Path.Combine(_directory, "nested", "deeper", "layout.json"));

        Assert.Null(store.Save(ShellLayout.Empty));
        Assert.True(File.Exists(store.Path));
    }

    [Fact]
    public void Saving_leaves_no_temporary_file_behind()
    {
        var store = new FileLayoutStore(LayoutPath);
        store.Save(ShellLayout.Empty);

        Assert.False(File.Exists(LayoutPath + ".tmp"));
    }

    [Fact]
    public void Saving_over_an_existing_layout_replaces_it()
    {
        var store = new FileLayoutStore(LayoutPath);

        store.Save(new ShellLayout { LastHost = "http://first:8080/" });
        store.Save(new ShellLayout { LastHost = "http://second:8080/" });

        Assert.Equal("http://second:8080/", store.Load().LastHost);
    }

    [Fact]
    public void A_save_failure_is_reported_rather_than_thrown()
    {
        // A directory where the file should be: writing can never succeed.
        Directory.CreateDirectory(LayoutPath);

        var problem = new FileLayoutStore(LayoutPath).Save(ShellLayout.Empty);

        Assert.NotNull(problem);
    }

    [Fact]
    public void The_default_path_sits_under_the_per_user_application_data_directory()
    {
        var path = FileLayoutStore.DefaultPath;

        Assert.Contains("ProjectCHAOS", path, StringComparison.Ordinal);
        Assert.EndsWith("layout.json", path, StringComparison.Ordinal);
        Assert.True(Path.IsPathRooted(path));
    }
}

/// <summary>
/// The startup failure panel. Its entire job is to replace a blank white
/// WebView2 with something an operator can act on.
/// </summary>
public sealed class StartupDiagnosticTests
{
    private static readonly HostEndpoints Endpoints =
        HostEndpoints.For(new Uri("http://chaos-node:8080"));

    /// <summary>
    /// Phrases that would assert a site condition the shell cannot possibly
    /// know while it has no connection. Claims about the platform being
    /// *stopped* are checked separately, because the summary says that phrase
    /// only inside an explicit negation.
    /// </summary>
    private static readonly string[] ForbiddenReassurances =
    {
        "no active alarms",
        "all clear",
        "everything is fine",
        "healthy",
        "operating normally",
    };

    [Fact]
    public void Gateway_diagnostic_states_what_is_known_and_what_is_not()
    {
        var diagnostic = StartupDiagnostic.GatewayUnreachable(
            Endpoints,
            attempts: 23,
            TimeSpan.FromSeconds(45),
            "No connection could be made because the target machine actively refused it",
            @"C:\ProgramData\ProjectCHAOS\logs",
            ShellMessages.ServiceName);

        Assert.False(diagnostic.PlatformStateKnown);

        // It must not claim the platform is down — only that it cannot see it.
        foreach (var phrase in ForbiddenReassurances)
        {
            Assert.DoesNotContain(phrase, diagnostic.Summary, StringComparison.OrdinalIgnoreCase);
        }

        Assert.Contains("cannot see the platform", diagnostic.Summary, StringComparison.Ordinal);

        // Any mention of the platform stopping must be inside a negation. The
        // shell has no evidence either way and must not imply that it does.
        var stoppedAt = diagnostic.Summary.IndexOf(
            "the platform has stopped", StringComparison.Ordinal);

        Assert.True(stoppedAt > 0, "expected the summary to address the 'is it down?' question");
        Assert.Contains(
            "does not by itself mean",
            diagnostic.Summary[..stoppedAt],
            StringComparison.Ordinal);
    }

    [Fact]
    public void Gateway_diagnostic_carries_everything_needed_to_act()
    {
        var diagnostic = StartupDiagnostic.GatewayUnreachable(
            Endpoints,
            attempts: 23,
            TimeSpan.FromSeconds(45),
            "connection refused",
            @"C:\ProgramData\ProjectCHAOS\logs",
            ShellMessages.ServiceName);

        var facts = string.Join("\n", diagnostic.Facts.Select(f => $"{f.Label}: {f.Value}"));

        Assert.Contains("http://chaos-node:8080/", facts, StringComparison.Ordinal);
        Assert.Contains("http://chaos-node:8080/health", facts, StringComparison.Ordinal);
        Assert.Contains("23", facts, StringComparison.Ordinal);
        Assert.Contains("connection refused", facts, StringComparison.Ordinal);
        Assert.Contains(@"C:\ProgramData\ProjectCHAOS\logs", facts, StringComparison.Ordinal);
        Assert.Contains(ShellMessages.ServiceName, facts, StringComparison.Ordinal);

        Assert.NotEmpty(diagnostic.NextSteps);
        Assert.Contains(diagnostic.NextSteps, s => s.Contains("sc query", StringComparison.Ordinal));
        Assert.Contains(diagnostic.NextSteps, s => s.Contains("--host", StringComparison.Ordinal));
    }

    [Fact]
    public void A_missing_error_is_shown_as_none_reported_not_as_blank()
    {
        var diagnostic = StartupDiagnostic.GatewayUnreachable(
            Endpoints, 1, TimeSpan.FromSeconds(5), null, "logs", "svc");

        Assert.Contains(diagnostic.Facts, f => f.Value == "(none reported)");
        Assert.All(diagnostic.Facts, f => Assert.False(string.IsNullOrWhiteSpace(f.Value)));
    }

    [Fact]
    public void WebView2_diagnostic_points_at_the_browser_as_a_way_through()
    {
        var diagnostic = StartupDiagnostic.WebViewRuntimeMissing(Endpoints, "not found");

        Assert.Contains("WebView2", diagnostic.Title, StringComparison.Ordinal);
        Assert.Contains("platform is unaffected", diagnostic.Summary, StringComparison.OrdinalIgnoreCase);

        var steps = string.Join("\n", diagnostic.NextSteps);
        Assert.Contains("http://chaos-node:8080/", steps, StringComparison.Ordinal);
        Assert.Contains("browser", steps, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Both_diagnostics_are_fully_populated()
    {
        foreach (var diagnostic in new[]
        {
            StartupDiagnostic.GatewayUnreachable(Endpoints, 1, TimeSpan.Zero, "e", "logs", "svc"),
            StartupDiagnostic.WebViewRuntimeMissing(Endpoints, "e"),
        })
        {
            Assert.False(string.IsNullOrWhiteSpace(diagnostic.Title));
            Assert.False(string.IsNullOrWhiteSpace(diagnostic.Summary));
            Assert.NotEmpty(diagnostic.Facts);
            Assert.NotEmpty(diagnostic.NextSteps);
            Assert.False(diagnostic.PlatformStateKnown);
        }
    }
}

/// <summary>
/// The wording that stops an operator believing a closed window means a stopped
/// platform.
/// </summary>
public sealed class ShellMessagesTests
{
    [Fact]
    public void Hiding_to_tray_says_the_platform_keeps_running()
    {
        Assert.Contains("still running", ShellMessages.HiddenToTrayTitle, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("Windows service", ShellMessages.HiddenToTrayBody, StringComparison.Ordinal);
    }

    [Fact]
    public void Exiting_says_what_exit_does_and_does_not_do()
    {
        Assert.Contains("keeps running", ShellMessages.ExitConfirmationBody, StringComparison.Ordinal);
        Assert.Contains("stop the Windows service", ShellMessages.ExitConfirmationBody, StringComparison.Ordinal);
    }

    [Fact]
    public void Unknown_service_status_is_never_worded_reassuringly()
    {
        Assert.Contains("unknown", ShellMessages.ServiceStatusUnknown, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("running fine", ShellMessages.ServiceStatusUnknown, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Acknowledge_is_documented_as_an_operator_action_not_a_shell_action()
    {
        Assert.Contains("never acknowledges for you", ShellMessages.AcknowledgeOpensPanel, StringComparison.Ordinal);
    }
}
