using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>
/// Builders for the launcher's inputs, so each test states only the fact it is
/// about.
/// </summary>
internal static class Facts
{
    public static readonly HostEndpoints Local =
        HostEndpoints.For(new Uri("http://127.0.0.1:8080/"));

    public static LauncherFacts New(
        GatewayProbe? gateway = null,
        ServiceProbe? service = null,
        HostExecutableProbe? executable = null,
        SetupSnapshot? setup = null,
        ShellSettings? settings = null) => new()
        {
            Endpoints = Local,
            Settings = settings ?? ShellSettings.Defaults,
            Gateway = gateway ?? GatewayProbe.NotProbed,
            Service = service ?? NoService,
            Executable = executable ?? NoExecutable,
            Setup = setup ?? SetupSnapshot.NotChecked,
        };

    public static readonly ServiceProbe NoService = new()
    {
        ServiceName = ShellMessages.ServiceName,
        State = PlatformServiceState.NotInstalled,
    };

    public static ServiceProbe Service(PlatformServiceState state, string? problem = null) => new()
    {
        ServiceName = ShellMessages.ServiceName,
        State = state,
        Problem = problem,
    };

    public static readonly HostExecutableProbe NoExecutable = new()
    {
        Searched = true,
        Path = null,
        Source = HostExecutableSource.None,
        Candidates = new[] { @"C:\Program Files\Project CHAOS\Chaos.Host.exe" },
    };

    public static readonly HostExecutableProbe Executable = new()
    {
        Searched = true,
        Path = @"C:\Program Files\Project CHAOS\Chaos.Host.exe",
        Source = HostExecutableSource.BesideShell,
    };

    public static GatewayProbe Silent(string error = "connection refused", int failures = 1) =>
        GatewayProbe.Silent(error, failures);

    public static GatewayProbe Answering(string backend = "up", string? status = null) =>
        GatewayProbe.Answered(
            new PlatformHealth
            {
                Status = status ?? (backend == "up" ? "ok" : "degraded"),
                Version = "0.5.0",
                Backend = backend,
            },
            backend == "up" ? 200 : 503,
            DateTimeOffset.UnixEpoch);

    public static SetupSnapshot SetupSaying(string state, params (string Id, string State, string Detail)[] steps)
    {
        var body = System.Text.Json.JsonSerializer.Serialize(new
        {
            state,
            steps = steps.Select(s => new { id = s.Id, state = s.State, detail = s.Detail }),
        });

        return SetupSnapshotReader.Read(body);
    }
}

/// <summary>
/// The launcher's judgement. Every case here is one an operator will meet on a
/// real node, and several of them are ones that cannot safely be reproduced on
/// a real node to check by hand.
/// </summary>
public sealed class LauncherStateMachineTests
{
    // ------------------------------------------------------------- structure --

    [Fact]
    public void Every_check_is_present_in_a_fixed_order_whatever_the_state()
    {
        foreach (var facts in EveryInterestingCase())
        {
            var view = LauncherStateMachine.Evaluate(facts);

            Assert.Equal(
                new[]
                {
                    LauncherCheckId.Gateway,
                    LauncherCheckId.WindowsService,
                    LauncherCheckId.HostExecutable,
                    LauncherCheckId.Backend,
                    LauncherCheckId.Setup,
                },
                view.Checks.Select(c => c.Id));
        }
    }

    [Fact]
    public void Every_check_says_something_an_operator_can_read()
    {
        foreach (var facts in EveryInterestingCase())
        {
            var view = LauncherStateMachine.Evaluate(facts);

            Assert.All(view.Checks, check =>
            {
                Assert.False(string.IsNullOrWhiteSpace(check.Label));
                Assert.False(string.IsNullOrWhiteSpace(check.Detail));
            });

            Assert.False(string.IsNullOrWhiteSpace(view.Headline));
            Assert.False(string.IsNullOrWhiteSpace(view.Summary));
        }
    }

    [Fact]
    public void An_action_that_cannot_be_taken_always_says_why()
    {
        // The rule the brief names: never silently disable a control.
        foreach (var facts in EveryInterestingCase())
        {
            var view = LauncherStateMachine.Evaluate(facts);

            Assert.All(view.Actions, offer =>
                Assert.False(
                    string.IsNullOrWhiteSpace(offer.Reason),
                    $"{offer.Action} in phase {view.Phase} was offered without a reason"));
        }
    }

    [Fact]
    public void At_most_one_action_is_primary_and_it_leads_the_list()
    {
        foreach (var facts in EveryInterestingCase())
        {
            var view = LauncherStateMachine.Evaluate(facts);
            var primaries = view.Actions.Where(a => a.IsPrimary).ToList();

            Assert.True(primaries.Count <= 1, $"phase {view.Phase} offered {primaries.Count} primaries");

            if (primaries.Count == 1)
            {
                Assert.Same(primaries[0], view.Actions[0]);
            }
        }
    }

    [Fact]
    public void A_primary_action_is_always_one_that_can_actually_be_invoked()
    {
        foreach (var facts in EveryInterestingCase())
        {
            var view = LauncherStateMachine.Evaluate(facts);
            if (view.Primary is { } primary)
            {
                Assert.True(primary.CanInvoke, $"{primary.Action} was primary but not invokable");
            }
        }
    }

    // --------------------------------------------------------------- honesty --

    [Fact]
    public void Nothing_is_ever_drawn_calm_before_it_has_been_checked()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New());

        Assert.Equal(LauncherPhase.Checking, view.Phase);
        Assert.DoesNotContain(view.Checks, c => c.State == CheckState.Pass);
        Assert.False(view.ConsoleIsUsable);
    }

    [Fact]
    public void An_unreachable_gateway_never_reads_as_a_stopped_platform()
    {
        // The gateway is silent but the service says it is running: the shell
        // knows only that it cannot see it, and must not resolve that either way.
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Running)));

        Assert.Equal(PlatformRunMode.Unknown, view.RunMode.Mode);
        Assert.Contains("cannot see the platform", view.Check(LauncherCheckId.Gateway)!.Detail, StringComparison.Ordinal);
        Assert.Contains("does not by itself mean", view.Check(LauncherCheckId.Gateway)!.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void Not_running_is_only_claimed_with_a_second_piece_of_evidence()
    {
        // Silent gateway plus an installed, positively stopped service.
        var known = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped)));

        Assert.Equal(PlatformRunMode.NotRunning, known.RunMode.Mode);

        // Silent gateway and no service at all: nothing is established.
        var unknown = LauncherStateMachine.Evaluate(Facts.New(gateway: Facts.Silent()));

        Assert.Equal(PlatformRunMode.Unknown, unknown.RunMode.Mode);
        Assert.Contains("not by itself evidence", unknown.RunMode.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void An_unknown_backend_word_is_never_rounded_up_to_healthy()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(backend: "quantum-superposition")));

        var backend = view.Check(LauncherCheckId.Backend)!;
        Assert.Equal(CheckState.Unknown, backend.State);
        Assert.Contains("does not know", backend.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void An_unknown_setup_word_is_never_rounded_up_to_ready()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(),
            setup: Facts.SetupSaying("almost-probably-fine")));

        Assert.Equal(CheckState.Unknown, view.Check(LauncherCheckId.Setup)!.State);
        Assert.Contains("will not guess", view.Setup.Summary, StringComparison.Ordinal);
    }

    [Fact]
    public void The_console_is_only_declared_usable_when_the_gateway_actually_answered()
    {
        foreach (var facts in EveryInterestingCase())
        {
            var view = LauncherStateMachine.Evaluate(facts);
            Assert.Equal(facts.Gateway.Reachable, view.ConsoleIsUsable);
        }
    }

    [Fact]
    public void Opening_the_console_is_refused_while_nothing_answers()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(gateway: Facts.Silent()));
        var console = view.Offer(LauncherAction.OpenConsole)!;

        Assert.Equal(ActionAvailability.Unavailable, console.Availability);
        Assert.Contains("blank window", console.Reason, StringComparison.Ordinal);
    }

    // ---------------------------------------------------------- run-mode --

    [Fact]
    public void A_platform_started_by_this_shell_is_flagged_as_dying_with_it()
    {
        var facts = Facts.New(gateway: Facts.Answering(), executable: Facts.Executable) with
        {
            ManagedChildRunning = true,
        };

        var view = LauncherStateMachine.Evaluate(facts);

        Assert.Equal(PlatformRunMode.ManagedByThisShell, view.RunMode.Mode);
        Assert.True(view.RunMode.ClosingTheShellStopsThePlatform);
        Assert.Equal(RunModeSeverity.Caution, view.RunMode.Severity);
        Assert.Contains("STOPS the platform", view.RunMode.Headline, StringComparison.Ordinal);
    }

    [Fact]
    public void A_platform_run_by_the_service_is_flagged_as_outliving_the_shell()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(),
            service: Facts.Service(PlatformServiceState.Running)));

        Assert.Equal(PlatformRunMode.WindowsService, view.RunMode.Mode);
        Assert.False(view.RunMode.ClosingTheShellStopsThePlatform);
        Assert.Contains("keeps running", view.RunMode.Headline, StringComparison.Ordinal);
    }

    [Fact]
    public void A_gateway_nobody_here_started_is_flagged_as_neither()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(gateway: Facts.Answering()));

        Assert.Equal(PlatformRunMode.Foreign, view.RunMode.Mode);
        Assert.False(view.RunMode.ClosingTheShellStopsThePlatform);
        Assert.Contains("did not start it", view.RunMode.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void The_two_run_modes_that_matter_never_share_wording()
    {
        var child = RunModeBanner.For(PlatformRunMode.ManagedByThisShell);
        var service = RunModeBanner.For(PlatformRunMode.WindowsService);

        Assert.NotEqual(child.Headline, service.Headline);
        Assert.NotEqual(child.Detail, service.Detail);
        Assert.NotEqual(child.ClosingTheShellStopsThePlatform, service.ClosingTheShellStopsThePlatform);

        // Only one of the two may be worded as safe to close.
        Assert.DoesNotContain("keeps running", child.Headline, StringComparison.OrdinalIgnoreCase);
    }

    // ----------------------------------------------------------- starting --

    [Fact]
    public void With_a_stopped_service_the_screen_asks_to_start_it()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped)));

        Assert.Equal(LauncherPhase.PlatformDown, view.Phase);

        var start = view.Primary!;
        Assert.Equal(LauncherAction.StartPlatform, start.Action);
        Assert.Contains("keeps controlling", start.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void With_no_service_but_an_executable_the_start_button_says_it_will_die_with_the_shell()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            executable: Facts.Executable));

        var start = view.Primary!;
        Assert.Equal(LauncherAction.StartPlatform, start.Action);
        Assert.Contains("under this shell", start.Label, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("WILL STOP WHEN YOU CLOSE THIS SHELL", start.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void The_service_is_preferred_over_the_executable_when_both_are_available()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped),
            executable: Facts.Executable));

        Assert.Contains("keeps controlling", view.Primary!.Reason, StringComparison.Ordinal);
        Assert.DoesNotContain("WILL STOP WHEN YOU CLOSE", view.Primary!.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void An_operator_can_choose_the_managed_child_over_an_installed_service()
    {
        var settings = ShellSettings.Defaults with { PreferWindowsService = false };

        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped),
            executable: Facts.Executable,
            settings: settings));

        Assert.Contains("WILL STOP WHEN YOU CLOSE THIS SHELL", view.Primary!.Reason, StringComparison.Ordinal);
        Assert.Contains("is installed but is not being used", view.Primary!.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void With_nothing_to_start_the_screen_says_exactly_what_is_missing()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(gateway: Facts.Silent()));

        Assert.Equal(LauncherPhase.Blocked, view.Phase);

        var start = view.Offer(LauncherAction.StartPlatform)!;
        Assert.Equal(ActionAvailability.Unavailable, start.Availability);
        Assert.Contains(ShellMessages.ServiceName, start.Reason, StringComparison.Ordinal);
        Assert.Contains(HostExecutableLocator.FileName, start.Reason, StringComparison.Ordinal);

        // Settings must stay reachable — it is how the operator fixes this.
        Assert.Equal(ActionAvailability.Available, view.Offer(LauncherAction.OpenSettings)!.Availability);
    }

    [Fact]
    public void Starting_is_not_offered_when_something_already_answers_on_that_address()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(),
            service: Facts.Service(PlatformServiceState.Stopped)));

        var start = view.Offer(LauncherAction.StartPlatform)!;
        Assert.Equal(ActionAvailability.Unavailable, start.Availability);
        Assert.Contains("already answering", start.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void A_gateway_on_another_machine_is_refused_rather_than_half_offered()
    {
        var settings = ShellSettings.Defaults with { HostAddress = "chaos-node" };
        var facts = Facts.New(
            gateway: Facts.Silent(),
            executable: Facts.Executable,
            settings: settings) with
        {
            AddressIsThisMachine = false,
        };

        var view = LauncherStateMachine.Evaluate(facts);

        Assert.Equal(LauncherPhase.Blocked, view.Phase);
        Assert.Equal(ActionAvailability.Unavailable, view.Offer(LauncherAction.StartPlatform)!.Availability);
        Assert.Contains("not this machine", view.Offer(LauncherAction.StartPlatform)!.Reason, StringComparison.Ordinal);
        Assert.Contains("chaos-node", view.Offer(LauncherAction.StartPlatform)!.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void A_service_that_is_starting_puts_the_screen_in_the_starting_phase()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.StartPending)));

        Assert.Equal(LauncherPhase.Starting, view.Phase);
        Assert.True(view.ShowProgress);
    }

    // ---------------------------------------------------------- elevation --

    [Fact]
    public void Controlling_a_service_unelevated_is_attempted_not_forbidden()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped)));

        var start = view.Offer(LauncherAction.StartPlatform)!;

        // Advisory, not blocked: service rights can be delegated, and refusing
        // to try would break a correctly locked-down node.
        Assert.Equal(ActionAvailability.NeedsElevation, start.Availability);
        Assert.True(start.CanInvoke);
        Assert.Null(view.Offer(LauncherAction.RelaunchElevated));
    }

    [Fact]
    public void Once_windows_has_actually_refused_the_shell_offers_to_restart_elevated()
    {
        var facts = Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped)) with
        {
            ElevationRefused = true,
        };

        var view = LauncherStateMachine.Evaluate(facts);
        var relaunch = view.Offer(LauncherAction.RelaunchElevated)!;

        Assert.Equal(ActionAvailability.Available, relaunch.Availability);
        Assert.Contains("whatever is running keeps running", relaunch.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void An_already_elevated_shell_is_never_told_to_elevate()
    {
        var facts = Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped)) with
        {
            IsElevated = true,
            ElevationRefused = true,
        };

        var view = LauncherStateMachine.Evaluate(facts);

        Assert.Null(view.Offer(LauncherAction.RelaunchElevated));
    }

    // -------------------------------------------------------------- setup --

    [Fact]
    public void A_platform_that_has_never_been_set_up_asks_to_be()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(),
            setup: Facts.SetupSaying("not_started")));

        Assert.Equal(LauncherPhase.SetupRequired, view.Phase);
        Assert.Equal(LauncherAction.RunSetup, view.Primary!.Action);
        Assert.Equal(SetupPresenter.SetUpNow, view.Primary!.Label);
    }

    [Fact]
    public void A_failed_setup_offers_a_retry_and_repeats_the_failing_step()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(),
            setup: Facts.SetupSaying(
                "failed",
                ("database", "ready", "Opened var/chaos.db."),
                ("points", "failed", "points.yaml names a sensor that no asset declares."))));

        Assert.Equal(LauncherPhase.SetupRequired, view.Phase);
        Assert.Equal(SetupPresenter.RetrySetup, view.Primary!.Label);
        Assert.Contains("names a sensor", view.Summary, StringComparison.Ordinal);
    }

    [Fact]
    public void While_setup_runs_it_cannot_be_asked_for_again()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(),
            setup: Facts.SetupSaying("running")));

        Assert.Equal(LauncherPhase.SettingUp, view.Phase);

        var run = view.Offer(LauncherAction.RunSetup)!;
        Assert.Equal(ActionAvailability.Unavailable, run.Availability);
        Assert.Contains("already running", run.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void A_host_too_old_to_report_setup_is_said_to_be_old_not_broken()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(),
            setup: SetupSnapshot.Absent("HTTP 404 from http://127.0.0.1:8080/host/setup")));

        // The platform is up, so the operator is not blocked.
        Assert.Equal(LauncherPhase.Ready, view.Phase);
        Assert.Equal(CheckState.Unknown, view.Check(LauncherCheckId.Setup)!.State);

        var run = view.Offer(LauncherAction.RunSetup)!;
        Assert.Equal(ActionAvailability.Unavailable, run.Availability);
        Assert.Contains("older than this shell", run.Reason, StringComparison.Ordinal);
        Assert.Contains("Update the platform", run.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void Setup_cannot_be_run_while_the_platform_is_unreachable()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped),
            setup: SetupSnapshot.Unreachable("connection refused")));

        var run = view.Offer(LauncherAction.RunSetup)!;
        Assert.Equal(ActionAvailability.Unavailable, run.Availability);
        Assert.Contains("Start the platform first", run.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void A_set_up_platform_leads_with_opening_the_console()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(),
            service: Facts.Service(PlatformServiceState.Running),
            setup: Facts.SetupSaying("ready", ("database", "ready", "Opened var/chaos.db."))));

        Assert.Equal(LauncherPhase.Ready, view.Phase);
        Assert.Equal(LauncherAction.OpenConsole, view.Primary!.Action);
        Assert.Equal(CheckState.Pass, view.Check(LauncherCheckId.Setup)!.State);
    }

    // ------------------------------------------------------------ backend --

    [Fact]
    public void A_gateway_up_with_its_backend_down_is_called_half_up()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(backend: "down"),
            service: Facts.Service(PlatformServiceState.Running)));

        Assert.Equal(LauncherPhase.BackendDown, view.Phase);
        Assert.Equal(CheckState.Fail, view.Check(LauncherCheckId.Backend)!.State);
        Assert.Contains("no alarms are being evaluated", view.Summary, StringComparison.Ordinal);

        // The console still loads — the gateway serves it — but with a caveat.
        var console = view.Offer(LauncherAction.OpenConsole)!;
        Assert.Equal(ActionAvailability.Available, console.Availability);
        Assert.Contains("no live data", console.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void A_backend_still_starting_is_waited_for_rather_than_declared_broken()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Answering(backend: "starting")));

        Assert.Equal(LauncherPhase.Starting, view.Phase);
        Assert.Equal(CheckState.Checking, view.Check(LauncherCheckId.Backend)!.State);
        Assert.True(view.ShowProgress);
    }

    [Fact]
    public void The_backend_is_unknown_rather_than_down_when_the_gateway_is_silent()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(gateway: Facts.Silent()));

        var backend = view.Check(LauncherCheckId.Backend)!;
        Assert.Equal(CheckState.Unknown, backend.State);
        Assert.Contains("no second way to ask", backend.Detail, StringComparison.Ordinal);
    }

    // -------------------------------------------------------------- stop --

    [Fact]
    public void Stopping_a_platform_this_shell_started_spells_out_the_consequence()
    {
        var facts = Facts.New(gateway: Facts.Answering(), executable: Facts.Executable) with
        {
            ManagedChildRunning = true,
        };

        var stop = LauncherStateMachine.Evaluate(facts).Offer(LauncherAction.StopPlatform)!;

        Assert.Equal(ActionAvailability.Available, stop.Availability);
        Assert.Contains("STOP BEING CONTROLLED", stop.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void A_platform_the_shell_did_not_start_cannot_be_stopped_and_says_so()
    {
        var stop = LauncherStateMachine
            .Evaluate(Facts.New(gateway: Facts.Answering()))
            .Offer(LauncherAction.StopPlatform)!;

        Assert.Equal(ActionAvailability.Unavailable, stop.Availability);
        Assert.Contains("no way to stop it", stop.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void No_stop_button_appears_when_there_is_nothing_running_to_stop()
    {
        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(),
            service: Facts.Service(PlatformServiceState.Stopped)));

        Assert.Null(view.Offer(LauncherAction.StopPlatform));
    }

    // ------------------------------------------------------------ notices --

    [Fact]
    public void A_configured_executable_that_has_gone_missing_is_reported_even_when_a_fallback_worked()
    {
        var executable = Facts.Executable with
        {
            Source = HostExecutableSource.BesideShell,
            Problem = @"The platform executable set in Settings is not there: D:\old\Chaos.Host.exe.",
        };

        var view = LauncherStateMachine.Evaluate(Facts.New(
            gateway: Facts.Silent(), executable: executable));

        Assert.Contains(view.Notices, n => n.Contains(@"D:\old\Chaos.Host.exe", StringComparison.Ordinal));
        Assert.Equal(CheckState.Warn, view.Check(LauncherCheckId.HostExecutable)!.State);
    }

    [Fact]
    public void The_result_of_the_last_action_is_carried_through_to_the_screen()
    {
        var facts = Facts.New(gateway: Facts.Silent()) with
        {
            LastActionMessage = "The service 'ChaosPlatform' did not reach Running within 30 s.",
        };

        Assert.Contains(
            LauncherStateMachine.Evaluate(facts).Notices,
            n => n.Contains("did not reach Running", StringComparison.Ordinal));
    }

    // ------------------------------------------------------------ coverage --

    /// <summary>
    /// The combinations the invariants above are checked against. Deliberately
    /// includes the contradictory ones — a running service with a silent
    /// gateway, a reachable gateway with no service — because those are the
    /// states that produce a confusing screen if nobody thought about them.
    /// </summary>
    private static IEnumerable<LauncherFacts> EveryInterestingCase()
    {
        var gateways = new[]
        {
            GatewayProbe.NotProbed,
            Facts.Silent(),
            Facts.Silent("timed out", failures: 9),
            Facts.Answering(),
            Facts.Answering(backend: "down"),
            Facts.Answering(backend: "starting"),
            Facts.Answering(backend: "who knows"),
        };

        var services = new[]
        {
            ServiceProbe.NotChecked(ShellMessages.ServiceName),
            Facts.NoService,
            Facts.Service(PlatformServiceState.Stopped),
            Facts.Service(PlatformServiceState.StartPending),
            Facts.Service(PlatformServiceState.Running),
            Facts.Service(PlatformServiceState.Paused),
            Facts.Service(PlatformServiceState.QueryFailed, "Access is denied."),
        };

        var executables = new[] { HostExecutableProbe.NotSearched, Facts.NoExecutable, Facts.Executable };

        var setups = new[]
        {
            SetupSnapshot.NotChecked,
            SetupSnapshot.Absent("HTTP 404"),
            SetupSnapshot.Unreachable("connection refused"),
            SetupSnapshot.Unreadable("not JSON"),
            Facts.SetupSaying("not_started"),
            Facts.SetupSaying("running"),
            Facts.SetupSaying("ready"),
            Facts.SetupSaying("failed", ("points", "failed", "bad yaml")),
            Facts.SetupSaying("needs_attention", ("bindings", "needs_attention", "two points unbound")),
            Facts.SetupSaying("mystery"),
        };

        foreach (var gateway in gateways)
        {
            foreach (var service in services)
            {
                foreach (var executable in executables)
                {
                    foreach (var setup in setups)
                    {
                        var basics = Facts.New(gateway, service, executable, setup);
                        yield return basics;
                        yield return basics with { ManagedChildRunning = true };
                        yield return basics with { IsElevated = true, ElevationRefused = true };
                        yield return basics with { ActionInProgress = true, ActionInProgressMessage = "Starting…" };
                        yield return basics with { AddressIsThisMachine = false };
                    }
                }
            }
        }
    }
}

/// <summary>
/// Whether the launcher pushes itself back in front of a working console.
/// </summary>
public sealed class LauncherReentryTests
{
    [Fact]
    public void One_dropped_poll_does_not_interrupt_an_operator()
    {
        Assert.False(LauncherReentry.ShouldReturn(1, null, false));
        Assert.False(LauncherReentry.ShouldReturn(2, null, false));
    }

    [Fact]
    public void A_run_of_failures_brings_the_launcher_back()
    {
        Assert.True(LauncherReentry.ShouldReturn(LauncherReentry.FailuresBeforeReturning, null, false));
    }

    [Fact]
    public void It_does_not_reappear_immediately_after_being_shown()
    {
        Assert.False(LauncherReentry.ShouldReturn(9, TimeSpan.FromSeconds(20), false));
        Assert.True(LauncherReentry.ShouldReturn(9, LauncherReentry.Quiet, false));
    }

    [Fact]
    public void An_operator_who_dismissed_it_is_not_nagged()
    {
        Assert.False(LauncherReentry.ShouldReturn(99, TimeSpan.FromHours(1), operatorDismissedIt: true));
    }
}
