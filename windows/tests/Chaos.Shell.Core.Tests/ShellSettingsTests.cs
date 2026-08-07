using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>The settings model and the address it composes.</summary>
public sealed class ShellSettingsTests
{
    [Fact]
    public void The_defaults_point_at_this_machine_on_the_documented_port()
    {
        var uri = ShellSettings.Defaults.TryComposeGatewayUri();

        Assert.NotNull(uri);
        Assert.Equal(HostEndpoints.DefaultGatewayHost, uri!.Host);
        Assert.Equal(HostEndpoints.DefaultGatewayPort, uri.Port);
        Assert.Equal(Uri.UriSchemeHttp, uri.Scheme);
    }

    [Fact]
    public void The_defaults_start_the_platform_so_one_double_click_is_enough()
    {
        Assert.True(ShellSettings.Defaults.AutoStartPlatform);
        Assert.True(ShellSettings.Defaults.PreferWindowsService);
    }

    [Fact]
    public void An_unbracketed_ipv6_address_is_composed_correctly()
    {
        var settings = ShellSettings.Defaults with { HostAddress = "fd00::1", HostPort = 8080 };

        Assert.Equal("http://[fd00::1]:8080/", settings.TryComposeGatewayUri()!.ToString());
    }

    [Fact]
    public void Https_is_honoured()
    {
        var settings = ShellSettings.Defaults with { UseHttps = true, HostAddress = "node", HostPort = 443 };

        Assert.Equal("https://node/", settings.TryComposeGatewayUri()!.ToString());
    }

    [Theory]
    [InlineData("", 8080)]
    [InlineData("node", 0)]
    [InlineData("node", 70000)]
    public void An_address_that_cannot_compose_returns_null_rather_than_something_wrong(
        string address, int port)
    {
        Assert.Null((ShellSettings.Defaults with { HostAddress = address, HostPort = port })
            .TryComposeGatewayUri());
    }

    [Fact]
    public void Endpoints_never_throw_and_fall_back_to_the_default()
    {
        // A shell that refuses to open because a preference is malformed locks
        // an operator out of their own control system.
        var broken = ShellSettings.Defaults with { HostAddress = string.Empty, HostPort = -1 };

        Assert.Equal(HostEndpoints.Default.BaseUri, broken.Endpoints().BaseUri);
    }

    [Fact]
    public void An_overriding_address_is_absorbed_without_losing_the_other_settings()
    {
        var settings = ShellSettings.Defaults with { Theme = ShellTheme.Dark, AnnunciatorFullScreen = true };

        var moved = settings.WithGateway(new Uri("https://chaos-node:9443/"));

        Assert.Equal("chaos-node", moved.HostAddress);
        Assert.Equal(9443, moved.HostPort);
        Assert.True(moved.UseHttps);
        Assert.Equal(ShellTheme.Dark, moved.Theme);
        Assert.True(moved.AnnunciatorFullScreen);
    }

    [Fact]
    public void An_ipv6_override_is_stored_unbracketed_so_the_settings_box_reads_naturally()
    {
        Assert.Equal("fd00::1", ShellSettings.Defaults.WithGateway(new Uri("http://[fd00::1]:8080/")).HostAddress);
    }
}

/// <summary>
/// Validating an edit.
/// </summary>
/// <remarks>
/// Each rule exists because the failure it prevents is silent: a relative data
/// directory that resolves against whatever working directory the service
/// control manager handed the platform, a port typo that leaves the shell
/// waiting forever on an address nothing is listening on.
/// </remarks>
public sealed class ShellSettingsValidatorTests
{
    [Fact]
    public void The_defaults_are_valid()
    {
        Assert.True(ShellSettingsValidator.Validate(ShellSettings.Defaults).IsValid);
    }

    [Fact]
    public void Whitespace_is_trimmed_and_blanks_become_unset()
    {
        var validation = ShellSettingsValidator.Validate(ShellSettings.Defaults with
        {
            HostAddress = "  chaos-node  ",
            DatabasePath = "   ",
            AnnunciatorMonitor = "",
        });

        Assert.True(validation.IsValid);
        Assert.Equal("chaos-node", validation.Normalised.HostAddress);
        Assert.Null(validation.Normalised.DatabasePath);
        Assert.Null(validation.Normalised.AnnunciatorMonitor);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-1)]
    [InlineData(65536)]
    public void An_impossible_port_is_named_as_the_port(int port)
    {
        var validation = ShellSettingsValidator.Validate(ShellSettings.Defaults with { HostPort = port });

        Assert.False(validation.IsValid);
        Assert.NotNull(validation.For(SettingsField.HostPort));
        Assert.Contains("1 and 65535", validation.For(SettingsField.HostPort)!, StringComparison.Ordinal);
    }

    [Fact]
    public void A_url_pasted_into_the_address_box_is_explained_rather_than_half_accepted()
    {
        var validation = ShellSettingsValidator.Validate(
            ShellSettings.Defaults with { HostAddress = "http://chaos-node:8080" });

        Assert.False(validation.IsValid);
        Assert.Contains("without http://", validation.For(SettingsField.HostAddress)!, StringComparison.Ordinal);
    }

    [Fact]
    public void A_port_typed_into_the_address_box_is_pointed_at_the_port_box()
    {
        var validation = ShellSettingsValidator.Validate(
            ShellSettings.Defaults with { HostAddress = "chaos-node:8080" });

        Assert.False(validation.IsValid);
        Assert.Contains("Port box", validation.For(SettingsField.HostAddress)!, StringComparison.Ordinal);
    }

    [Fact]
    public void A_bare_ipv6_address_is_not_mistaken_for_a_port_in_the_wrong_box()
    {
        Assert.True(ShellSettingsValidator
            .Validate(ShellSettings.Defaults with { HostAddress = "fd00::1" })
            .IsValid);
    }

    [Fact]
    public void An_empty_address_is_named_and_told_what_to_put_there()
    {
        var validation = ShellSettingsValidator.Validate(
            ShellSettings.Defaults with { HostAddress = "  " });

        Assert.False(validation.IsValid);
        Assert.Contains(
            HostEndpoints.DefaultGatewayHost,
            validation.For(SettingsField.HostAddress)!,
            StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(@"var\chaos.db")]
    [InlineData("data/chaos.db")]
    [InlineData("chaos.db")]
    public void A_relative_storage_path_is_refused_with_the_reason(string path)
    {
        var validation = ShellSettingsValidator.Validate(
            ShellSettings.Defaults with { DatabasePath = path });

        Assert.False(validation.IsValid);
        Assert.Contains(
            "not somewhere you can find it again",
            validation.For(SettingsField.DatabasePath)!,
            StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(@"C:\ProgramData\Project CHAOS\chaos.db")]
    [InlineData(@"D:/chaos/chaos.db")]
    [InlineData(@"\\nas\chaos\chaos.db")]
    [InlineData("/var/lib/chaos/chaos.db")]
    public void A_full_path_is_accepted_in_any_of_the_forms_windows_uses(string path)
    {
        Assert.True(ShellSettingsValidator
            .Validate(ShellSettings.Defaults with { DatabasePath = path })
            .IsValid);
    }

    [Fact]
    public void A_connection_url_is_accepted_as_a_database_location()
    {
        // Refusing one would push an operator with a non-SQLite database back
        // into a config file, which is the thing this page exists to end.
        Assert.True(ShellSettingsValidator
            .Validate(ShellSettings.Defaults with { DatabasePath = "postgresql://node/chaos" })
            .IsValid);
    }

    [Fact]
    public void A_service_name_that_is_really_a_path_is_refused()
    {
        var validation = ShellSettingsValidator.Validate(
            ShellSettings.Defaults with { ServiceName = @"C:\services\ChaosPlatform" });

        Assert.False(validation.IsValid);
        Assert.Contains("not a path", validation.For(SettingsField.ServiceName)!, StringComparison.Ordinal);
    }

    [Fact]
    public void An_empty_service_name_names_the_installers_default()
    {
        var validation = ShellSettingsValidator.Validate(
            ShellSettings.Defaults with { ServiceName = string.Empty });

        Assert.False(validation.IsValid);
        Assert.Contains(
            ShellMessages.ServiceName,
            validation.For(SettingsField.ServiceName)!,
            StringComparison.Ordinal);
    }

    [Fact]
    public void Every_problem_names_a_field_the_page_can_point_at()
    {
        var validation = ShellSettingsValidator.Validate(new ShellSettings
        {
            HostAddress = string.Empty,
            HostPort = 0,
            ServiceName = string.Empty,
            DatabasePath = "relative.db",
            DataDirectory = "also-relative",
            HostExecutablePath = "Chaos.Host.exe",
        });

        Assert.False(validation.IsValid);
        Assert.All(validation.Problems, problem =>
        {
            Assert.False(string.IsNullOrWhiteSpace(problem.Field));
            Assert.False(string.IsNullOrWhiteSpace(problem.Message));
        });

        Assert.Equal(6, validation.Problems.Count);
    }

    [Fact]
    public void What_the_operator_typed_survives_a_rejection()
    {
        // The page keeps showing the bad value so it can be corrected rather
        // than silently reverted.
        var validation = ShellSettingsValidator.Validate(
            ShellSettings.Defaults with { HostAddress = "chaos-node:8080" });

        Assert.Equal("chaos-node:8080", validation.Normalised.HostAddress);
    }
}

/// <summary>Saying what a save will and will not do.</summary>
public sealed class SettingsChangeImpactTests
{
    [Fact]
    public void Changing_nothing_says_nothing_changed()
    {
        var impact = SettingsChangeImpact.Evaluate(
            ShellSettings.Defaults, ShellSettings.Defaults, PlatformRunMode.WindowsService);

        Assert.False(impact.AnythingChanged);
        Assert.False(impact.RequiresPlatformRestart);
        Assert.Equal("Nothing changed.", impact.Headline);
    }

    [Fact]
    public void Moving_the_database_under_a_managed_child_needs_a_restart()
    {
        var impact = SettingsChangeImpact.Evaluate(
            ShellSettings.Defaults,
            ShellSettings.Defaults with { DatabasePath = @"D:\chaos\chaos.db" },
            PlatformRunMode.ManagedByThisShell);

        Assert.True(impact.RequiresPlatformRestart);
        Assert.Contains(impact.Notes, n => n.Contains("has to be restarted", StringComparison.Ordinal));
        Assert.Contains("needs the platform restarted", impact.Headline, StringComparison.Ordinal);
    }

    [Fact]
    public void Moving_the_database_under_a_windows_service_warns_that_it_will_be_ignored()
    {
        // This is the honesty case: the shell does not rewrite the service's
        // configuration, so an operator who thinks they moved their database
        // has not.
        var impact = SettingsChangeImpact.Evaluate(
            ShellSettings.Defaults,
            ShellSettings.Defaults with { DatabasePath = @"D:\chaos\chaos.db" },
            PlatformRunMode.WindowsService);

        Assert.False(impact.RequiresPlatformRestart);
        Assert.Contains(impact.Warnings, w =>
            w.Contains("reads its own configuration", StringComparison.Ordinal)
            && w.Contains("unaffected", StringComparison.Ordinal));
    }

    [Fact]
    public void Moving_the_address_under_a_managed_child_moves_the_platform_too()
    {
        var impact = SettingsChangeImpact.Evaluate(
            ShellSettings.Defaults,
            ShellSettings.Defaults with { HostPort = 9090 },
            PlatformRunMode.ManagedByThisShell);

        Assert.True(impact.RequiresReconnect);
        Assert.True(impact.RequiresPlatformRestart);
    }

    [Fact]
    public void Moving_the_address_under_a_service_only_moves_where_the_shell_looks()
    {
        var impact = SettingsChangeImpact.Evaluate(
            ShellSettings.Defaults,
            ShellSettings.Defaults with { HostAddress = "chaos-node" },
            PlatformRunMode.WindowsService);

        Assert.True(impact.RequiresReconnect);
        Assert.False(impact.RequiresPlatformRestart);
        Assert.Contains(impact.Warnings, w =>
            w.Contains("only changes where the shell looks", StringComparison.Ordinal));
    }

    [Fact]
    public void Turning_off_the_service_preference_says_the_platform_will_die_with_the_shell()
    {
        var impact = SettingsChangeImpact.Evaluate(
            ShellSettings.Defaults,
            ShellSettings.Defaults with { PreferWindowsService = false },
            PlatformRunMode.WindowsService);

        Assert.Contains(impact.Notes, n => n.Contains("STOPS when this shell exits", StringComparison.Ordinal));
    }

    [Fact]
    public void Starting_with_windows_is_distinguished_from_starting_the_platform()
    {
        var impact = SettingsChangeImpact.Evaluate(
            ShellSettings.Defaults,
            ShellSettings.Defaults with { StartWithWindows = true },
            PlatformRunMode.WindowsService);

        Assert.Contains(impact.Notes, n =>
            n.Contains("opens the window, not the platform", StringComparison.Ordinal));
    }

    [Fact]
    public void Every_changed_field_produces_a_line_the_operator_can_read()
    {
        var after = new ShellSettings
        {
            HostAddress = "chaos-node",
            HostPort = 9090,
            UseHttps = true,
            AutoStartPlatform = false,
            StartWithWindows = true,
            PreferWindowsService = false,
            ServiceName = "OtherService",
            HostExecutablePath = @"D:\chaos\Chaos.Host.exe",
            DatabasePath = @"D:\chaos\chaos.db",
            DataDirectory = @"D:\chaos\data",
            Theme = ShellTheme.Dark,
            AnnunciatorMonitor = "2",
            AnnunciatorFullScreen = true,
        };

        foreach (var mode in Enum.GetValues<PlatformRunMode>())
        {
            var impact = SettingsChangeImpact.Evaluate(ShellSettings.Defaults, after, mode);

            Assert.True(impact.AnythingChanged);
            Assert.All(impact.Notes, n => Assert.False(string.IsNullOrWhiteSpace(n)));
            Assert.All(impact.Warnings, w => Assert.False(string.IsNullOrWhiteSpace(w)));
            Assert.False(string.IsNullOrWhiteSpace(impact.Headline));
        }
    }
}

/// <summary>Reading and writing the settings file.</summary>
public sealed class ShellSettingsStoreTests : IDisposable
{
    private readonly string _directory = Path.Combine(
        Path.GetTempPath(), "chaos-settings-tests-" + Guid.NewGuid().ToString("N"));

    private string SettingsPath => Path.Combine(_directory, "settings.json");

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }

    [Fact]
    public void Settings_round_trip()
    {
        var store = new FileShellSettingsStore(SettingsPath);
        var settings = new ShellSettings
        {
            HostAddress = "chaos-node",
            HostPort = 9090,
            UseHttps = true,
            AutoStartPlatform = false,
            StartWithWindows = true,
            PreferWindowsService = false,
            ServiceName = "ChaosPlatform",
            HostExecutablePath = @"D:\chaos\Chaos.Host.exe",
            DatabasePath = @"D:\chaos\chaos.db",
            DataDirectory = @"D:\chaos\data",
            Theme = ShellTheme.Dark,
            AnnunciatorMonitor = "2",
            AnnunciatorFullScreen = true,
        };

        Assert.Null(store.Save(settings));

        var loaded = store.Load();
        Assert.True(loaded.Existed);
        Assert.Null(loaded.Problem);
        Assert.Equal(settings, loaded.Settings);
    }

    [Fact]
    public void A_first_run_is_distinguishable_from_a_damaged_file()
    {
        var first = new FileShellSettingsStore(SettingsPath).Load();

        Assert.False(first.Existed);
        Assert.Null(first.Problem);
        Assert.Equal(ShellSettings.Defaults, first.Settings);
    }

    [Theory]
    [InlineData("")]
    [InlineData("{ not json")]
    [InlineData("null")]
    public void A_damaged_file_yields_usable_defaults_and_says_so(string content)
    {
        // Silently reverting to defaults, and letting an operator believe their
        // configured gateway is in use, is the failure this reports.
        Directory.CreateDirectory(_directory);
        File.WriteAllText(SettingsPath, content);

        var load = new FileShellSettingsStore(SettingsPath).Load();

        Assert.True(load.Existed);
        Assert.NotNull(load.Problem);
        Assert.Equal(ShellSettings.Defaults, load.Settings);
    }

    [Fact]
    public void A_file_from_a_newer_shell_is_refused_rather_than_guessed_at()
    {
        Directory.CreateDirectory(_directory);
        File.WriteAllText(SettingsPath, """{"v": 99, "hostAddress": "chaos-node", "hostPort": 1}""");

        var load = new FileShellSettingsStore(SettingsPath).Load();

        Assert.NotNull(load.Problem);
        Assert.Contains("different version", load.Problem!, StringComparison.Ordinal);
        Assert.Equal(ShellSettings.Defaults, load.Settings);
    }

    [Fact]
    public void A_stored_value_that_is_no_longer_usable_is_reported_not_hidden()
    {
        Directory.CreateDirectory(_directory);
        File.WriteAllText(SettingsPath, """{"v": 1, "hostAddress": "chaos-node", "hostPort": 0}""");

        var load = new FileShellSettingsStore(SettingsPath).Load();

        Assert.NotNull(load.Problem);
        Assert.Contains("not usable", load.Problem!, StringComparison.Ordinal);
    }

    [Fact]
    public void Saving_creates_the_directory_and_leaves_no_temporary_file()
    {
        var store = new FileShellSettingsStore(Path.Combine(_directory, "a", "b", "settings.json"));

        Assert.Null(store.Save(ShellSettings.Defaults));
        Assert.True(File.Exists(store.Path));
        Assert.False(File.Exists(store.Path + ".tmp"));
    }

    [Fact]
    public void A_save_failure_is_reported_rather_than_thrown()
    {
        Directory.CreateDirectory(SettingsPath);

        Assert.NotNull(new FileShellSettingsStore(SettingsPath).Save(ShellSettings.Defaults));
    }

    [Fact]
    public void The_settings_file_sits_beside_the_layout_but_is_not_the_same_file()
    {
        // Layout is rewritten on every window move; settings are deliberate
        // choices that must not be lost to a racing layout write.
        Assert.NotEqual(FileLayoutStore.DefaultPath, FileShellSettingsStore.DefaultPath);
        Assert.Equal(
            Path.GetDirectoryName(FileLayoutStore.DefaultPath),
            Path.GetDirectoryName(FileShellSettingsStore.DefaultPath));
        Assert.EndsWith("settings.json", FileShellSettingsStore.DefaultPath, StringComparison.Ordinal);
    }
}

/// <summary>Registering the shell to start at sign-in.</summary>
public sealed class StartupRegistrationTests
{
    private const string Exe = @"C:\Program Files\Project CHAOS\Chaos.Shell.exe";

    [Fact]
    public void The_command_is_quoted_so_a_space_in_the_path_cannot_run_the_wrong_thing()
    {
        Assert.Equal($"\"{Exe}\"", StartupRegistration.CommandFor(Exe));
    }

    [Fact]
    public void Asking_for_it_when_nothing_is_registered_registers_it()
    {
        var plan = StartupRegistration.Plan(desired: true, currentValue: null, Exe);

        Assert.Equal(StartupRegistrationAction.Register, plan.Action);
        Assert.Equal(StartupRegistration.CommandFor(Exe), plan.Value);
    }

    [Fact]
    public void Asking_for_what_is_already_there_does_nothing()
    {
        var plan = StartupRegistration.Plan(true, StartupRegistration.CommandFor(Exe), Exe);

        Assert.Equal(StartupRegistrationAction.None, plan.Action);
    }

    [Fact]
    public void A_stale_entry_from_an_old_install_is_replaced_and_named()
    {
        // Otherwise Windows quietly starts yesterday's build every morning.
        var plan = StartupRegistration.Plan(true, @"""D:\old\Chaos.Shell.exe""", Exe);

        Assert.Equal(StartupRegistrationAction.Update, plan.Action);
        Assert.Contains(@"D:\old\Chaos.Shell.exe", plan.Explanation, StringComparison.Ordinal);
        Assert.Contains(Exe, plan.Explanation, StringComparison.Ordinal);
    }

    [Fact]
    public void Turning_it_off_removes_it_and_says_the_platform_is_untouched()
    {
        var plan = StartupRegistration.Plan(false, StartupRegistration.CommandFor(Exe), Exe);

        Assert.Equal(StartupRegistrationAction.Unregister, plan.Action);
        Assert.Null(plan.Value);
        Assert.Contains("platform is unaffected", plan.Explanation, StringComparison.Ordinal);
    }

    [Fact]
    public void Turning_off_something_that_was_never_on_does_nothing()
    {
        Assert.Equal(
            StartupRegistrationAction.None,
            StartupRegistration.Plan(false, null, Exe).Action);
    }

    [Fact]
    public void It_registers_for_this_user_only()
    {
        // A machine-wide autorun would need administrator rights for what is
        // purely a convenience.
        Assert.StartsWith(@"Software\Microsoft\Windows", StartupRegistration.RunKeyPath, StringComparison.Ordinal);
        Assert.DoesNotContain("HKEY", StartupRegistration.RunKeyPath, StringComparison.Ordinal);
    }

    [Fact]
    public void Every_plan_explains_itself()
    {
        foreach (var desired in new[] { true, false })
        {
            foreach (var current in new[] { null, @"""D:\old\Chaos.Shell.exe""", StartupRegistration.CommandFor(Exe) })
            {
                var plan = StartupRegistration.Plan(desired, current, Exe);
                Assert.False(string.IsNullOrWhiteSpace(plan.Explanation));
            }
        }
    }
}

/// <summary>Where the gateway address in force came from.</summary>
public sealed class HostUrlResolverSettingsTests
{
    [Fact]
    public void The_saved_address_is_used_when_nothing_overrides_it()
    {
        var settings = ShellSettings.Defaults with { HostAddress = "chaos-node", HostPort = 9090 };

        var resolution = HostUrlResolver.Resolve(null, null, settings);

        Assert.Equal(HostUrlSource.Settings, resolution.Source);
        Assert.Equal("http://chaos-node:9090/", resolution.Endpoints.BaseUri.ToString());
    }

    [Fact]
    public void A_command_line_argument_wins_over_the_saved_address()
    {
        var settings = ShellSettings.Defaults with { HostAddress = "chaos-node" };

        var resolution = HostUrlResolver.Resolve("other-node:7000", null, settings);

        Assert.Equal(HostUrlSource.CommandLine, resolution.Source);
        Assert.Equal("http://other-node:7000/", resolution.Endpoints.BaseUri.ToString());
    }

    [Fact]
    public void The_environment_wins_over_the_saved_address_but_not_the_command_line()
    {
        var settings = ShellSettings.Defaults with { HostAddress = "chaos-node" };

        Assert.Equal(
            HostUrlSource.Environment,
            HostUrlResolver.Resolve(null, "http://env-node:8080", settings).Source);

        Assert.Equal(
            HostUrlSource.CommandLine,
            HostUrlResolver.Resolve("cli-node", "http://env-node:8080", settings).Source);
    }

    [Fact]
    public void An_unusable_saved_address_falls_back_and_says_so()
    {
        var settings = ShellSettings.Defaults with { HostAddress = string.Empty };

        var resolution = HostUrlResolver.Resolve(null, null, settings);

        Assert.Equal(HostEndpoints.Default.BaseUri, resolution.Endpoints.BaseUri);
        Assert.NotNull(resolution.Problem);
        Assert.Contains("Open Settings", resolution.Problem!, StringComparison.Ordinal);
    }

    [Fact]
    public void Where_the_address_came_from_can_be_said_out_loud()
    {
        // An operator whose Settings address appears to be ignored has to be
        // able to see why.
        var settings = ShellSettings.Defaults with { HostAddress = "chaos-node" };

        Assert.Contains(
            "--host argument",
            HostUrlResolver.DescribeSource(HostUrlResolver.Resolve("other", null, settings)),
            StringComparison.Ordinal);

        Assert.Contains(
            HostUrlResolver.EnvironmentVariable,
            HostUrlResolver.DescribeSource(HostUrlResolver.Resolve(null, "http://x:8080", settings)),
            StringComparison.Ordinal);

        Assert.Contains(
            "from Settings",
            HostUrlResolver.DescribeSource(HostUrlResolver.Resolve(null, null, settings)),
            StringComparison.Ordinal);

        Assert.Contains(
            "no address has been set",
            HostUrlResolver.DescribeSource(HostUrlResolver.Resolve(null, null, null)),
            StringComparison.Ordinal);
    }

    [Fact]
    public void The_two_argument_form_still_behaves_as_it_did()
    {
        Assert.Equal(HostUrlSource.Default, HostUrlResolver.Resolve(null, null).Source);
    }
}
