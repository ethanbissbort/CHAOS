using Chaos.Host.Configuration;
using Chaos.Host.Setup;
using Chaos.Host.Tests.Support;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Tests;

/// <summary>
/// The setup state machine, driven against a fake platform CLI.
/// </summary>
/// <remarks>
/// No interpreter, no database, no network. Everything the coordinator does to
/// the outside world goes through <see cref="IPlatformCommandRunner"/>, so what
/// it asked for — and, more importantly, what it declined to ask for — is
/// directly observable.
/// </remarks>
public sealed class PlatformSetupCoordinatorTests : IDisposable
{
    private readonly string _stateDirectory = Path.Combine(
        Path.GetTempPath(), "chaos-coordinator-tests-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        try
        {
            if (Directory.Exists(_stateDirectory))
            {
                Directory.Delete(_stateDirectory, recursive: true);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Not a test failure.
        }
    }

    [Fact]
    public async Task A_fresh_machine_is_set_up_using_the_platforms_own_commands()
    {
        var runner = new FakePlatformCommandRunner((verb, call) => verb switch
        {
            "status" when call == 1 => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("test", force: false).Accepted);
        await coordinator.Current;

        var snapshot = coordinator.Snapshot;
        Assert.Equal(SetupState.Ready, snapshot.State);

        // The tested paths, in order, and nothing invented in between.
        Assert.Equal(
            ["status --json", "init-db", "load-all --skip-missing", "status --json"],
            runner.Invocations);

        Assert.Null(snapshot.Failure);
        Assert.Null(snapshot.CurrentActivity);
        Assert.Equal("Set the platform up and verified it.", snapshot.LastActivity);
        Assert.NotNull(snapshot.LastActivityUtc);
        Assert.NotNull(snapshot.CompletedUtc);
        Assert.NotNull(snapshot.LastCheckedUtc);
        Assert.Equal(4, snapshot.Commands.Count);
        Assert.All(snapshot.Commands, command => Assert.Equal(0, command.ExitCode));
    }

    [Fact]
    public async Task An_already_set_up_machine_is_a_no_op_that_says_so()
    {
        var runner = new FakePlatformCommandRunner((_, _) =>
            FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()));

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("test", force: false).Accepted);
        await coordinator.Current;

        var snapshot = coordinator.Snapshot;
        Assert.Equal(SetupState.Ready, snapshot.State);

        // Read-only. No init-db, and above all no second import over a loaded
        // registry.
        Assert.Equal(["status --json"], runner.Invocations);
        Assert.Equal("Checked; nothing to do. The platform was already set up.", snapshot.LastActivity);
        Assert.Contains("already set up", snapshot.LastActivity!, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Running_it_twice_over_leaves_the_second_run_with_nothing_to_do()
    {
        var loaded = false;
        var runner = new FakePlatformCommandRunner((verb, _) =>
        {
            if (verb == "load-all")
            {
                loaded = true;
            }

            return verb == "status"
                ? FakePlatformCommandRunner.Ok(loaded ? PlatformStatusJson.Loaded() : PlatformStatusJson.Fresh())
                : FakePlatformCommandRunner.Ok();
        });

        using var coordinator = Build(runner);

        Assert.True(coordinator.RequestRun("first", force: false).Accepted);
        await coordinator.Current;
        Assert.Equal(SetupState.Ready, coordinator.Snapshot.State);

        Assert.True(coordinator.RequestRun("second", force: false).Accepted);
        await coordinator.Current;

        Assert.Equal(SetupState.Ready, coordinator.Snapshot.State);
        Assert.Equal(2, coordinator.Snapshot.RunCount);

        // Exactly one init-db and one load-all across both runs: the second run
        // looked, found the platform set up, and stopped.
        Assert.Single(runner.Invocations.Where(line => line == "init-db"));
        Assert.Single(runner.Invocations.Where(line => line == "load-all --skip-missing"));
    }

    [Fact]
    public async Task A_second_request_while_one_is_running_does_not_start_a_second_run()
    {
        var gate = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var runner = new FakePlatformCommandRunner((verb, call) => verb switch
        {
            "status" when call == 1 => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()),
            _ => FakePlatformCommandRunner.Ok(),
        })
        {
            Gate = gate,
        };

        using var coordinator = Build(runner);

        var first = coordinator.RequestRun("first", force: false);
        Assert.True(first.Accepted);

        // Wait until the run is genuinely inside the runner before racing it.
        await runner.Entered.Task.WaitAsync(TimeSpan.FromSeconds(10));

        var second = coordinator.RequestRun("second", force: false);
        var third = coordinator.RequestRun("third", force: true);

        Assert.False(second.Accepted);
        Assert.Equal(SetupRunAcceptance.AlreadyRunning, second.Reason);
        Assert.False(third.Accepted);
        Assert.Equal(SetupRunAcceptance.AlreadyRunning, third.Reason);

        // Not even a forced request queues behind one. Two concurrent imports
        // into the same database is the failure this guard exists for.
        Assert.Equal(1, coordinator.RunsStarted);

        runner.Gate = null;
        gate.SetResult();
        await coordinator.Current;

        Assert.Equal(SetupState.Ready, coordinator.Snapshot.State);
        Assert.Equal(1, coordinator.Snapshot.RunCount);
    }

    [Fact]
    public async Task A_failing_step_surfaces_the_real_error_and_leaves_the_state_failed()
    {
        var runner = new FakePlatformCommandRunner((verb, _) => verb switch
        {
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "init-db" => FakePlatformCommandRunner.Fails(
                1,
                "The platform CLI exited 1 (a runtime failure).\nchaos: error: Could not create tables in "
              + "sqlite:////var/chaos/homestead.db: unable to open database file",
                "chaos: error: Could not create tables in sqlite:////var/chaos/homestead.db: unable to open database file",
                "chaos: hint: Check CHAOS_DATABASE_URL and that the target directory is writable."),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("test", force: false).Accepted);
        await coordinator.Current;

        var snapshot = coordinator.Snapshot;
        Assert.Equal(SetupState.Failed, snapshot.State);

        var failure = snapshot.Failure;
        Assert.NotNull(failure);
        Assert.Equal(SetupStepIds.Schema, failure!.Step);
        Assert.Equal(1, failure.ExitCode);

        // The platform's own words, not a paraphrase.
        Assert.Contains("unable to open database file", failure.Message, StringComparison.Ordinal);
        Assert.Contains("init-db", failure.Command!, StringComparison.Ordinal);
        Assert.Contains("CHAOS_DATABASE_URL", string.Join(" ", failure.StandardErrorTail), StringComparison.Ordinal);
        Assert.False(string.IsNullOrWhiteSpace(failure.Remedy));
        Assert.NotNull(failure.LogPath);

        // The load never ran, so nothing was half-imported on top of a failure.
        Assert.DoesNotContain("load-all --skip-missing", runner.Invocations);
        Assert.Equal(SetupStepState.Failed, snapshot.Steps.Single(s => s.Id == SetupStepIds.Schema).State);
    }

    [Fact]
    public async Task A_platform_that_cannot_be_launched_at_all_fails_with_the_resolver_report()
    {
        var runner = new FakePlatformCommandRunner((_, _) => FakePlatformCommandRunner.CouldNotStart(
            "No Python interpreter on PATH (python3, python) and no embedded runtime."));

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("test", force: false).Accepted);
        await coordinator.Current;

        var snapshot = coordinator.Snapshot;
        Assert.Equal(SetupState.Failed, snapshot.State);
        Assert.Equal("probe", snapshot.Failure!.Step);
        Assert.Contains("No Python interpreter", snapshot.Failure.Message, StringComparison.Ordinal);
        Assert.Contains("does not know whether the database exists", snapshot.Failure.Remedy,
            StringComparison.Ordinal);

        // Nothing was changed, and nothing pretends otherwise.
        Assert.Equal(["status --json"], runner.Invocations);
    }

    [Fact]
    public async Task Output_that_is_not_the_expected_payload_fails_rather_than_being_guessed_at()
    {
        var runner = new FakePlatformCommandRunner((_, _) =>
            FakePlatformCommandRunner.Ok("Homestead Digital Twin 0.4.0\n  node role : primary\n"));

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("test", force: false).Accepted);
        await coordinator.Current;

        Assert.Equal(SetupState.Failed, coordinator.Snapshot.State);
        Assert.Contains("did not print a JSON object", coordinator.Snapshot.Failure!.Message,
            StringComparison.Ordinal);
        Assert.Equal(["status --json"], runner.Invocations);
    }

    [Fact]
    public void Auto_setup_disabled_means_it_never_runs()
    {
        var runner = new FakePlatformCommandRunner((_, _) =>
            throw new InvalidOperationException("The platform CLI must not be launched with auto-setup off."));

        using var coordinator = Build(runner, autoSetup: false);
        var acceptance = coordinator.RequestRun("startup", force: false);

        Assert.False(acceptance.Accepted);
        Assert.Equal(SetupRunAcceptance.AutoSetupDisabled, acceptance.Reason);
        Assert.Empty(runner.Invocations);
        Assert.Equal(0, coordinator.RunsStarted);

        // "not_started" is a statement about this gateway, not a claim about the
        // database.
        Assert.Equal(SetupState.NotStarted, coordinator.Snapshot.State);
        Assert.False(coordinator.Snapshot.AutoSetupEnabled);
    }

    [Fact]
    public async Task An_operator_can_still_force_a_run_on_a_node_with_auto_setup_off()
    {
        var runner = new FakePlatformCommandRunner((verb, call) => verb switch
        {
            "status" when call == 1 => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var coordinator = Build(runner, autoSetup: false);
        Assert.True(coordinator.RequestRun("api", force: true).Accepted);
        await coordinator.Current;

        Assert.Equal(SetupState.Ready, coordinator.Snapshot.State);
    }

    [Fact]
    public async Task An_existing_but_wrong_database_is_reported_and_never_touched()
    {
        var wrong = PlatformStatusJson.Build(
            "sqlite:////var/chaos/homestead.db",
            missingTables: ["telemetry_samples"],
            counts: new Dictionary<string, int>(StringComparer.Ordinal)
            {
                ["assets"] = 90,
                ["points"] = 701,
                ["alarm_definitions"] = 40,
                ["commands"] = 3,
            });

        var runner = new FakePlatformCommandRunner((_, _) => FakePlatformCommandRunner.Ok(wrong));

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("startup", force: false).Accepted);
        await coordinator.Current;

        var snapshot = coordinator.Snapshot;
        Assert.Equal(SetupState.NeedsAttention, snapshot.State);
        Assert.Null(snapshot.Failure);

        // The whole point: read, report, and change nothing.
        Assert.Equal(["status --json"], runner.Invocations);
        Assert.Contains("reports the mismatch rather than repairing it", snapshot.Summary, StringComparison.Ordinal);
        Assert.Equal("Checked; stopped without changing anything.", snapshot.LastActivity);
    }

    [Fact]
    public async Task Forcing_past_needs_attention_still_only_runs_the_additive_commands()
    {
        var wrong = PlatformStatusJson.Build(
            "sqlite:////var/chaos/homestead.db",
            missingTables: ["telemetry_samples"],
            counts: new Dictionary<string, int>(StringComparer.Ordinal) { ["assets"] = 90, ["commands"] = 3 });

        var runner = new FakePlatformCommandRunner((verb, call) => verb switch
        {
            "status" when call == 1 => FakePlatformCommandRunner.Ok(wrong),
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("api", force: true).Accepted);
        await coordinator.Current;

        Assert.Equal(SetupState.Ready, coordinator.Snapshot.State);

        // Forced or not, the only commands that exist are the two that add.
        Assert.Equal(
            ["status --json", "init-db", "load-all --skip-missing", "status --json"],
            runner.Invocations);
    }

    [Fact]
    public async Task Commands_that_succeed_without_producing_a_complete_database_do_not_report_ready()
    {
        var runner = new FakePlatformCommandRunner((verb, _) => verb == "status"
            ? FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh())
            : FakePlatformCommandRunner.Ok());

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("test", force: false).Accepted);
        await coordinator.Current;

        // Both commands exited zero. "Ready" is still refused, because the
        // platform's own verification says the database is not set up.
        Assert.Equal(SetupState.NeedsAttention, coordinator.Snapshot.State);
        Assert.Equal("Ran setup, but the database still does not look complete.",
            coordinator.Snapshot.LastActivity);
    }

    [Fact]
    public async Task A_successful_run_is_recorded_so_the_registry_can_be_compared_with_data_later()
    {
        var dataDirectory = Path.Combine(_stateDirectory, "data");
        Directory.CreateDirectory(dataDirectory);
        await File.WriteAllTextAsync(Path.Combine(dataDirectory, "asset_register.yaml"), "assets: []\n");

        var runner = new FakePlatformCommandRunner((verb, call) => verb switch
        {
            "status" when call == 1 => FakePlatformCommandRunner.Ok(PlatformStatusJson.Fresh()),
            "status" => FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()),
            _ => FakePlatformCommandRunner.Ok(),
        });

        using var first = Build(runner, dataDirectory: dataDirectory);
        Assert.True(first.RequestRun("test", force: false).Accepted);
        await first.Current;
        Assert.Equal(SetupState.Ready, first.Snapshot.State);

        var record = SetupRecord.TryRead(first.RecordPath);
        Assert.NotNull(record);
        Assert.NotNull(record!.DesignPackageFingerprint);

        // A second host over the same state file now knows the registry matches
        // what is on disk.
        var second = new FakePlatformCommandRunner((_, _) =>
            FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()));
        using var rerun = Build(second, dataDirectory: dataDirectory);
        Assert.True(rerun.RequestRun("test", force: false).Accepted);
        await rerun.Current;

        Assert.Equal(SetupStepState.Present,
            rerun.Snapshot.Steps.Single(step => step.Id == SetupStepIds.DesignPackage).State);

        // Change the design package and it reports the drift, without importing.
        await File.WriteAllTextAsync(Path.Combine(dataDirectory, "asset_register.yaml"), "assets: [one]\n");

        var third = new FakePlatformCommandRunner((_, _) =>
            FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()));
        using var drifted = Build(third, dataDirectory: dataDirectory);
        Assert.True(drifted.RequestRun("test", force: false).Accepted);
        await drifted.Current;

        var step = drifted.Snapshot.Steps.Single(s => s.Id == SetupStepIds.DesignPackage);
        Assert.Equal(SetupStepState.Partial, step.State);
        Assert.Equal(SetupState.Ready, drifted.Snapshot.State);
        Assert.Equal(["status --json"], third.Invocations);
    }

    [Fact]
    public async Task The_transcript_records_every_command_and_its_exit_code()
    {
        var runner = new FakePlatformCommandRunner((_, _) =>
            FakePlatformCommandRunner.Ok(PlatformStatusJson.Loaded()));

        using var coordinator = Build(runner);
        Assert.True(coordinator.RequestRun("test", force: false).Accepted);
        await coordinator.Current;

        var logPath = coordinator.Snapshot.LogPath;
        Assert.NotNull(logPath);

        var transcript = await File.ReadAllTextAsync(logPath!);
        Assert.Contains("setup run: trigger=test", transcript, StringComparison.Ordinal);
        Assert.Contains("setup run finished: ready", transcript, StringComparison.Ordinal);
    }

    private PlatformSetupCoordinator Build(
        FakePlatformCommandRunner runner,
        bool autoSetup = true,
        string? dataDirectory = null)
    {
        Directory.CreateDirectory(_stateDirectory);

        var options = new ChaosHostOptions
        {
            AutoSetup = autoSetup,
            DataDirectory = dataDirectory,
            SetupLogPath = Path.Combine(_stateDirectory, "chaos-setup.log"),
            SetupStateFile = Path.Combine(_stateDirectory, "chaos-setup-state.json"),
            SetupTimeout = TimeSpan.FromSeconds(30),
            SetupProbeTimeout = TimeSpan.FromSeconds(15),
            SetupCommandTimeout = TimeSpan.FromSeconds(20),
        };

        return new PlatformSetupCoordinator(
            runner,
            Options.Create(options),
            SetupLogFactory.Create(options),
            NullLogger<PlatformSetupCoordinator>.Instance);
    }
}
