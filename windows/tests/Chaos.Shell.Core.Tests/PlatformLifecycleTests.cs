using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>
/// Reading the service control manager. These cases matter because reproducing
/// them by hand means deliberately breaking a node's control plane.
/// </summary>
public sealed class ServiceStateInterpreterTests
{
    [Theory]
    [InlineData(1, PlatformServiceState.Stopped)]
    [InlineData(2, PlatformServiceState.StartPending)]
    [InlineData(3, PlatformServiceState.StopPending)]
    [InlineData(4, PlatformServiceState.Running)]
    [InlineData(5, PlatformServiceState.ContinuePending)]
    [InlineData(6, PlatformServiceState.PausePending)]
    [InlineData(7, PlatformServiceState.Paused)]
    public void Windows_status_codes_map_to_states(int status, PlatformServiceState expected) =>
        Assert.Equal(expected, ServiceStateInterpreter.FromServiceControllerStatus(status));

    [Theory]
    [InlineData(0)]
    [InlineData(8)]
    [InlineData(-1)]
    [InlineData(int.MaxValue)]
    public void A_status_code_we_do_not_know_is_never_read_as_running(int status)
    {
        var state = ServiceStateInterpreter.FromServiceControllerStatus(status);

        Assert.Equal(PlatformServiceState.QueryFailed, state);
        Assert.NotEqual(PlatformServiceState.Running, state);
    }

    [Fact]
    public void Not_installed_and_could_not_ask_are_different_answers()
    {
        var missing = new ServiceProbe { ServiceName = "ChaosPlatform", State = PlatformServiceState.NotInstalled };
        var blind = new ServiceProbe
        {
            ServiceName = "ChaosPlatform",
            State = PlatformServiceState.QueryFailed,
            Problem = "Access is denied.",
        };

        Assert.False(missing.Installed);
        Assert.False(blind.Installed);

        Assert.Contains("No Windows service called", ServiceStateInterpreter.Describe(missing), StringComparison.Ordinal);

        var blindText = ServiceStateInterpreter.Describe(blind);
        Assert.Contains("Access is denied.", blindText, StringComparison.Ordinal);
        Assert.Contains("says nothing about whether", blindText, StringComparison.Ordinal);
    }

    [Fact]
    public void Every_state_produces_a_sentence()
    {
        foreach (var state in Enum.GetValues<PlatformServiceState>())
        {
            var text = ServiceStateInterpreter.Describe(
                new ServiceProbe { ServiceName = "ChaosPlatform", State = state });

            Assert.False(string.IsNullOrWhiteSpace(text));
        }
    }

    [Fact]
    public void A_start_that_reached_running_says_the_platform_now_outlives_the_shell()
    {
        var outcome = ServiceStateInterpreter.InterpretStart(
            PlatformServiceState.Running, TimeSpan.FromSeconds(4), TimeSpan.FromSeconds(30));

        Assert.Equal(ServiceControlResult.Succeeded, outcome.Result);
        Assert.True(outcome.Worked);
        Assert.Contains("keeps running when this shell closes", outcome.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void A_start_that_timed_out_does_not_pronounce_it_failed()
    {
        var outcome = ServiceStateInterpreter.InterpretStart(
            PlatformServiceState.StartPending, TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30));

        Assert.Equal(ServiceControlResult.TimedOut, outcome.Result);
        Assert.False(outcome.Worked);

        // The shell stopped waiting; it did not cancel anything, and must not
        // imply that it did.
        Assert.Contains("may still be starting", outcome.Message, StringComparison.Ordinal);
        Assert.Contains("it did not cancel the start", outcome.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Access_denied_is_reported_with_a_way_through()
    {
        var outcome = ServiceStateInterpreter.InterpretStart(
            PlatformServiceState.Stopped, TimeSpan.Zero, TimeSpan.FromSeconds(30), win32Error: 5);

        Assert.Equal(ServiceControlResult.AccessDenied, outcome.Result);
        Assert.True(outcome.SuggestElevation);
        Assert.Contains("administrator", outcome.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Access_denied_is_recognised_from_the_message_when_no_code_survives()
    {
        // The service controller wrapper surfaces this as an InvalidOperation
        // with the Win32 code buried; catching the wording as well means the
        // operator still gets an offer to elevate rather than a raw string.
        var outcome = ServiceStateInterpreter.InterpretStart(
            PlatformServiceState.Stopped,
            TimeSpan.Zero,
            TimeSpan.FromSeconds(30),
            errorMessage: "Cannot open ChaosPlatform service on computer '.'. Access is denied");

        Assert.Equal(ServiceControlResult.AccessDenied, outcome.Result);
        Assert.True(outcome.SuggestElevation);
    }

    [Fact]
    public void A_missing_service_is_reported_as_missing_not_as_denied()
    {
        var outcome = ServiceStateInterpreter.InterpretStart(
            PlatformServiceState.Unknown, TimeSpan.Zero, TimeSpan.FromSeconds(30), win32Error: 1060);

        Assert.Equal(ServiceControlResult.NotInstalled, outcome.Result);
        Assert.False(outcome.SuggestElevation);
        Assert.Contains("no Windows service called", outcome.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Starting_something_already_running_is_not_reported_as_a_failure()
    {
        var outcome = ServiceStateInterpreter.InterpretStart(
            PlatformServiceState.Running, TimeSpan.Zero, TimeSpan.FromSeconds(30), win32Error: 1056);

        Assert.Equal(ServiceControlResult.AlreadyThere, outcome.Result);
        Assert.True(outcome.Worked);
    }

    [Fact]
    public void A_successful_stop_states_plainly_that_the_site_is_no_longer_controlled()
    {
        var outcome = ServiceStateInterpreter.InterpretStop(
            PlatformServiceState.Stopped, TimeSpan.FromSeconds(3), TimeSpan.FromSeconds(30));

        Assert.Equal(ServiceControlResult.Succeeded, outcome.Result);
        Assert.Contains("THE PLATFORM IS NOW STOPPED", outcome.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void A_stop_that_timed_out_refuses_to_claim_either_outcome()
    {
        var outcome = ServiceStateInterpreter.InterpretStop(
            PlatformServiceState.StopPending, TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30));

        Assert.Equal(ServiceControlResult.TimedOut, outcome.Result);
        Assert.Contains("Do not assume", outcome.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void No_outcome_ever_leaves_the_operator_without_a_sentence()
    {
        var codes = new int?[] { null, 5, 1053, 1056, 1060, 1062, 999 };

        foreach (var code in codes)
        {
            foreach (var state in Enum.GetValues<PlatformServiceState>())
            {
                var start = ServiceStateInterpreter.InterpretStart(
                    state, TimeSpan.FromSeconds(1), TimeSpan.FromSeconds(30), code, "boom");
                var stop = ServiceStateInterpreter.InterpretStop(
                    state, TimeSpan.FromSeconds(1), TimeSpan.FromSeconds(30), code, "boom");

                Assert.False(string.IsNullOrWhiteSpace(start.Message));
                Assert.False(string.IsNullOrWhiteSpace(stop.Message));
            }
        }
    }
}

/// <summary>Finding the gateway executable.</summary>
public sealed class HostExecutableLocatorTests
{
    private const string ShellDirectory = "/opt/chaos/shell";

    private static Func<string, bool> Exists(params string[] present)
    {
        var set = new HashSet<string>(present.Select(Normalize), StringComparer.Ordinal);
        return path => set.Contains(Normalize(path));
    }

    /// <summary>
    /// Collapses the "a/../b" forms Path.Combine leaves behind, and settles on
    /// one separator so a candidate assembled on Windows compares equal to the
    /// POSIX-shaped fixture it is meant to match.
    /// </summary>
    /// <remarks>
    /// Deliberately not <c>Path.GetFullPath(path, "/")</c>, which is what this
    /// used to be. That asks a question about the machine running the test, and
    /// gets two different answers: on Linux it resolves against the real root,
    /// and on Windows it throws — "Basepath argument is not fully qualified",
    /// because a path rooted with no drive letter is rooted but not fully
    /// qualified. These strings are invented and are only ever compared with
    /// other invented strings, so the collapsing is done here, in a way that
    /// gives the same answer on either OS.
    /// </remarks>
    private static string Normalize(string path)
    {
        var rooted = path.Length > 0 && (path[0] == '/' || path[0] == '\\');
        var segments = new List<string>();

        foreach (var segment in path.Split('/', '\\'))
        {
            if (segment.Length == 0 || segment == ".")
            {
                continue;
            }

            if (segment == "..")
            {
                // A ".." above the root has nowhere to go and is dropped, which
                // is what every filesystem does with it.
                if (segments.Count > 0)
                {
                    segments.RemoveAt(segments.Count - 1);
                }
                else if (!rooted)
                {
                    segments.Add(segment);
                }

                continue;
            }

            segments.Add(segment);
        }

        return (rooted ? "/" : string.Empty) + string.Join('/', segments);
    }

    [Fact]
    public void The_configured_path_wins_over_anything_lying_beside_the_shell()
    {
        var probe = HostExecutableLocator.Locate(
            "/srv/chaos/Chaos.Host.exe",
            ShellDirectory,
            Exists("/srv/chaos/Chaos.Host.exe", "/opt/chaos/shell/Chaos.Host.exe"));

        Assert.Equal("/srv/chaos/Chaos.Host.exe", probe.Path);
        Assert.Equal(HostExecutableSource.Configured, probe.Source);
        Assert.Null(probe.Problem);
    }

    [Fact]
    public void A_configured_directory_is_understood_as_the_folder_holding_it()
    {
        var probe = HostExecutableLocator.Locate(
            "/srv/chaos",
            ShellDirectory,
            Exists("/srv/chaos/Chaos.Host.exe"));

        Assert.Equal(HostExecutableSource.Configured, probe.Source);
        Assert.EndsWith(HostExecutableLocator.FileName, probe.Path!, StringComparison.Ordinal);
    }

    [Fact]
    public void A_configured_path_that_has_gone_missing_is_reported_even_though_a_fallback_worked()
    {
        // Silently running a different executable than the one an operator
        // configured is how the wrong build ends up controlling a homestead.
        var probe = HostExecutableLocator.Locate(
            "/srv/gone/Chaos.Host.exe",
            ShellDirectory,
            Exists("/opt/chaos/shell/Chaos.Host.exe"));

        Assert.Equal(HostExecutableSource.BesideShell, probe.Source);
        Assert.NotNull(probe.Problem);
        Assert.Contains("/srv/gone/Chaos.Host.exe", probe.Problem!, StringComparison.Ordinal);
        Assert.Contains("/srv/gone/Chaos.Host.exe", HostExecutableLocator.Describe(probe), StringComparison.Ordinal);
    }

    [Fact]
    public void The_shells_own_folder_is_the_ordinary_case()
    {
        var probe = HostExecutableLocator.Locate(
            null, ShellDirectory, Exists("/opt/chaos/shell/Chaos.Host.exe"));

        Assert.Equal(HostExecutableSource.BesideShell, probe.Source);
    }

    [Fact]
    public void A_development_checkout_is_found_without_configuring_anything()
    {
        var probe = HostExecutableLocator.Locate(
            null,
            "/src/repo/windows/src/Chaos.Shell/bin/x64/Debug/net10.0-windows10.0.26100.0",
            Exists("/src/repo/windows/src/Chaos.Host/bin/Debug/net10.0/Chaos.Host.exe"));

        Assert.Equal(HostExecutableSource.DevelopmentCheckout, probe.Source);
    }

    [Fact]
    public void Debug_beats_release_in_a_checkout()
    {
        var probe = HostExecutableLocator.Locate(
            null,
            "/src/repo/windows/src/Chaos.Shell/bin/x64/Debug/net10.0-windows10.0.26100.0",
            Exists(
                "/src/repo/windows/src/Chaos.Host/bin/Debug/net10.0/Chaos.Host.exe",
                "/src/repo/windows/src/Chaos.Host/bin/Release/net10.0/Chaos.Host.exe"));

        Assert.Contains("Debug", probe.Path!, StringComparison.Ordinal);
    }

    [Fact]
    public void Not_finding_it_lists_where_it_looked()
    {
        var probe = HostExecutableLocator.Locate(null, ShellDirectory, _ => false);

        Assert.False(probe.Found);
        Assert.True(probe.Searched);
        Assert.NotEmpty(probe.Candidates);
        Assert.Contains("was not found", HostExecutableLocator.Describe(probe), StringComparison.Ordinal);
        Assert.Contains("Settings", HostExecutableLocator.Describe(probe), StringComparison.Ordinal);
    }

    [Fact]
    public void The_search_terminates_at_the_filesystem_root()
    {
        // Walking up must not loop when GetDirectoryName stops changing.
        var probe = HostExecutableLocator.Locate(null, "/", _ => false);

        Assert.False(probe.Found);
        Assert.NotEmpty(probe.Candidates);
    }
}

/// <summary>Choosing how to start, and saying so first.</summary>
public sealed class PlatformStartPlannerTests
{
    [Fact]
    public void The_managed_child_plan_carries_the_listen_address_and_storage_settings()
    {
        var settings = ShellSettings.Defaults with
        {
            HostPort = 9090,
            DatabasePath = @"D:\chaos\chaos.db",
            DataDirectory = @"D:\chaos\data",
        };

        var plan = PlatformStartPlanner.Plan(
            Facts.New(executable: Facts.Executable, settings: settings));

        Assert.Equal(StartMethod.ManagedChild, plan.Method);
        Assert.Equal("http://0.0.0.0:9090", plan.Environment[PlatformStartPlanner.ListenUrlVariable]);
        Assert.Equal("sqlite:///D:/chaos/chaos.db", plan.Environment[PlatformStartPlanner.DatabaseUrlVariable]);
        Assert.Equal(@"D:\chaos\data", plan.Environment[PlatformStartPlanner.DataDirectoryVariable]);
    }

    [Fact]
    public void A_database_setting_that_is_already_a_url_is_passed_through_untouched()
    {
        Assert.Equal(
            "postgresql://node/chaos",
            PlatformStartPlanner.ToDatabaseUrl("postgresql://node/chaos"));
    }

    [Fact]
    public void The_service_plan_warns_that_storage_settings_will_not_be_applied()
    {
        // The service reads its own configuration. A setting that looks saved
        // but is ignored is worse than one that was refused.
        var settings = ShellSettings.Defaults with { DatabasePath = @"D:\chaos\chaos.db" };

        var plan = PlatformStartPlanner.Plan(Facts.New(
            service: Facts.Service(PlatformServiceState.Stopped), settings: settings));

        Assert.Equal(StartMethod.WindowsService, plan.Method);
        Assert.Contains("will NOT be applied", plan.Explanation, StringComparison.Ordinal);
        Assert.Contains("database location", plan.Explanation, StringComparison.Ordinal);
    }

    [Fact]
    public void Each_plan_states_the_run_mode_it_produces()
    {
        Assert.Equal(
            PlatformRunMode.WindowsService,
            PlatformStartPlanner.Plan(Facts.New(service: Facts.Service(PlatformServiceState.Stopped)))
                .ResultingRunMode);

        Assert.Equal(
            PlatformRunMode.ManagedByThisShell,
            PlatformStartPlanner.Plan(Facts.New(executable: Facts.Executable)).ResultingRunMode);
    }

    [Fact]
    public void A_refused_plan_always_says_why()
    {
        var plan = PlatformStartPlanner.Plan(Facts.New());

        Assert.False(plan.CanStart);
        Assert.False(string.IsNullOrWhiteSpace(plan.Refusal));
        Assert.Equal(plan.Refusal, plan.Explanation);
    }

    [Fact]
    public void A_service_the_shell_could_not_read_is_reported_rather_than_assumed_absent()
    {
        var plan = PlatformStartPlanner.Plan(Facts.New(
            service: Facts.Service(PlatformServiceState.QueryFailed, "Access is denied.")));

        Assert.False(plan.CanStart);
        Assert.Contains("could not read the state", plan.Refusal!, StringComparison.Ordinal);
        Assert.Contains("Access is denied.", plan.Refusal!, StringComparison.Ordinal);
    }
}

/// <summary>Whether the operator has the rights, and what to tell them.</summary>
public sealed class ElevationTests
{
    [Fact]
    public void Service_control_is_advisory_before_it_has_been_refused()
    {
        var decision = Elevation.ForServiceControl(isElevated: false, "start", "ChaosPlatform");

        Assert.Equal(ElevationNeed.Advisory, decision.Need);
        Assert.False(decision.OfferRelaunch);
    }

    [Fact]
    public void An_elevated_shell_is_told_it_already_has_what_it_needs()
    {
        var decision = Elevation.ForServiceControl(isElevated: true, "start", "ChaosPlatform");

        Assert.Equal(ElevationNeed.NotRequired, decision.Need);
        Assert.False(decision.OfferRelaunch);
    }

    [Fact]
    public void After_a_refusal_the_relaunch_is_offered()
    {
        var decision = Elevation.AfterAccessDenied(isElevated: false, "start the service");

        Assert.Equal(ElevationNeed.Required, decision.Need);
        Assert.True(decision.OfferRelaunch);
    }

    [Fact]
    public void An_administrator_who_is_still_refused_is_not_told_to_do_it_again()
    {
        var decision = Elevation.AfterAccessDenied(isElevated: true, "start the service");

        Assert.False(decision.OfferRelaunch);
        Assert.Contains("already running as", decision.Explanation, StringComparison.Ordinal);
    }

    [Fact]
    public void The_managed_child_is_documented_as_needing_nothing()
    {
        // This is the way through when nobody with administrator rights is
        // available, so it has to be stated rather than merely be true.
        var decision = Elevation.ForManagedChild();

        Assert.Equal(ElevationNeed.NotRequired, decision.Need);
        Assert.Contains("does not need administrator", decision.Explanation, StringComparison.Ordinal);
    }
}

/// <summary>Is the configured address this machine?</summary>
public sealed class LocalAddressTests
{
    [Theory]
    [InlineData("127.0.0.1")]
    [InlineData("localhost")]
    [InlineData("::1")]
    [InlineData("[::1]")]
    [InlineData("0.0.0.0")]
    [InlineData("")]
    [InlineData(null)]
    public void Loopback_and_nothing_are_this_machine(string? address) =>
        Assert.True(LocalAddress.IsThisMachine(address, "CHAOS-NODE"));

    [Theory]
    [InlineData("chaos-node")]
    [InlineData("CHAOS-NODE")]
    [InlineData("chaos-node.lan")]
    public void Our_own_name_in_any_form_is_this_machine(string address) =>
        Assert.True(LocalAddress.IsThisMachine(address, "chaos-node"));

    [Fact]
    public void One_of_our_own_addresses_is_this_machine()
    {
        Assert.True(LocalAddress.IsThisMachine(
            "192.168.1.40", "chaos-node", new[] { "192.168.1.40", "fe80::1" }));
    }

    [Theory]
    [InlineData("other-node")]
    [InlineData("192.168.1.99")]
    public void Anything_else_is_not(string address) =>
        Assert.False(LocalAddress.IsThisMachine(address, "chaos-node", new[] { "192.168.1.40" }));

    [Fact]
    public void A_name_is_not_matched_against_a_machine_that_has_no_name()
    {
        Assert.False(LocalAddress.IsThisMachine("chaos-node", string.Empty));
    }
}

/// <summary>The captured output of a platform this shell started.</summary>
public sealed class PlatformLogBufferTests
{
    [Fact]
    public void Lines_come_back_oldest_first()
    {
        var buffer = new PlatformLogBuffer();
        buffer.Add(LogStream.Output, "first");
        buffer.Add(LogStream.Error, "second");

        Assert.Equal(new[] { "first", "second" }, buffer.Snapshot().Select(l => l.Text));
    }

    [Fact]
    public void Blank_lines_are_dropped_rather_than_kept_as_blanks()
    {
        var buffer = new PlatformLogBuffer();
        buffer.Add(LogStream.Output, null);
        buffer.Add(LogStream.Output, "   ");
        buffer.Add(LogStream.Output, string.Empty);

        Assert.Equal(0, buffer.Count);
    }

    [Fact]
    public void A_platform_stuck_in_a_restart_loop_cannot_grow_the_buffer_without_limit()
    {
        var buffer = new PlatformLogBuffer(capacity: 10);
        for (var i = 0; i < 1000; i++)
        {
            buffer.Add(LogStream.Output, $"line {i}");
        }

        Assert.Equal(10, buffer.Count);
        Assert.Equal(990, buffer.Dropped);
        Assert.Equal("line 999", buffer.Snapshot()[^1].Text);
    }

    [Fact]
    public void What_was_dropped_is_stated_rather_than_a_partial_log_passing_as_whole()
    {
        var buffer = new PlatformLogBuffer(capacity: 2);
        buffer.Add(LogStream.Output, "a");
        buffer.Add(LogStream.Output, "b");
        buffer.Add(LogStream.Output, "c");

        Assert.Contains("1 earlier line dropped", buffer.Render(), StringComparison.Ordinal);
    }

    [Fact]
    public void A_clean_buffer_renders_without_a_dropped_notice()
    {
        var buffer = new PlatformLogBuffer();
        buffer.Add(LogStream.Shell, "starting");

        Assert.DoesNotContain("dropped", buffer.Render(), StringComparison.Ordinal);
        Assert.Contains("starting", buffer.Render(), StringComparison.Ordinal);
    }

    [Fact]
    public void Standard_error_is_distinguishable_from_standard_output()
    {
        var buffer = new PlatformLogBuffer();
        buffer.Add(LogStream.Error, "bad");
        buffer.Add(LogStream.Output, "good");

        var rendered = buffer.Render();
        Assert.Contains("err  bad", rendered, StringComparison.Ordinal);
        Assert.Contains("out  good", rendered, StringComparison.Ordinal);
    }

    [Fact]
    public void Writing_from_many_threads_loses_nothing_and_stays_inside_the_capacity()
    {
        // The child's stdout and stderr arrive on different threads.
        var buffer = new PlatformLogBuffer(capacity: 5000);

        Parallel.For(0, 2000, i => buffer.Add(i % 2 == 0 ? LogStream.Output : LogStream.Error, $"line {i}"));

        Assert.Equal(2000, buffer.Count);
        Assert.Equal(0, buffer.Dropped);
    }

    [Fact]
    public void A_capacity_below_one_is_refused_rather_than_silently_disabling_the_log()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => new PlatformLogBuffer(0));
    }
}
