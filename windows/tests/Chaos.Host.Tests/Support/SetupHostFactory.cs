using Chaos.Host.Setup;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace Chaos.Host.Tests.Support;

/// <summary>
/// The real gateway pipeline with first-run setup enabled and its only contact
/// with Python replaced by a fake.
/// </summary>
/// <remarks>
/// Separate from <see cref="ChaosHostFactory"/> because setup is the one
/// subsystem those tests deliberately turn off. Everything else — the endpoints,
/// the hosted-service ordering, <c>/health</c>'s composition — is the shipping
/// pipeline, not a reconstruction of it.
/// </remarks>
internal sealed class SetupHostFactory : WebApplicationFactory<Program>
{
    private readonly Dictionary<string, string?> _settings;
    private readonly FakePlatformCommandRunner _runner;
    private readonly string _stateDirectory;

    private SetupHostFactory(
        Dictionary<string, string?> settings,
        FakePlatformCommandRunner runner,
        string stateDirectory)
    {
        _settings = settings;
        _runner = runner;
        _stateDirectory = stateDirectory;
    }

    /// <summary>The fake platform CLI this host is wired to.</summary>
    public FakePlatformCommandRunner Runner => _runner;

    /// <summary>The temp directory holding this host's setup log and state file.</summary>
    public string StateDirectory => _stateDirectory;

    /// <summary>The live coordinator, for asserting on run counts.</summary>
    public PlatformSetupCoordinator Coordinator => Services.GetRequiredService<PlatformSetupCoordinator>();

    /// <summary>Starts a builder.</summary>
    /// <param name="runner">The fake platform CLI.</param>
    /// <returns>The builder.</returns>
    public static Builder For(FakePlatformCommandRunner runner) => new(runner);

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        foreach (var (key, value) in _settings)
        {
            builder.UseSetting(key, value);
        }

        builder.ConfigureServices(services =>
        {
            services.RemoveAll<IPlatformCommandRunner>();
            services.AddSingleton<IPlatformCommandRunner>(_runner);
        });
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);

        if (!disposing)
        {
            return;
        }

        try
        {
            if (Directory.Exists(_stateDirectory))
            {
                Directory.Delete(_stateDirectory, recursive: true);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // A temp directory that will not delete is not a test failure.
        }
    }

    /// <summary>Fluent configuration for <see cref="SetupHostFactory"/>.</summary>
    internal sealed class Builder
    {
        private readonly Dictionary<string, string?> _settings = new(StringComparer.OrdinalIgnoreCase);
        private readonly FakePlatformCommandRunner _runner;
        private readonly string _stateDirectory;

        internal Builder(FakePlatformCommandRunner runner)
        {
            _runner = runner;
            _stateDirectory = Path.Combine(
                Path.GetTempPath(),
                "chaos-setup-tests-" + Guid.NewGuid().ToString("N"));

            _settings["Chaos:BackendUrl"] = StubBackend.ReserveClosedLoopbackUrl();
            _settings["Chaos:ListenUrl"] = "http://127.0.0.1:0";
            _settings["Chaos:WebRootPath"] = Path.Combine(Path.GetTempPath(), "chaos-setup-tests-absent-webroot");
            _settings["Chaos:BackendStartTimeout"] = "00:00:00.100";
            _settings["Chaos:BackendHealthInterval"] = "00:00:01";
            _settings["Chaos:BackendHealthTimeout"] = "00:00:02";
            _settings["Chaos:BackendHealthMaxBackoff"] = "00:00:01";
            _settings["Chaos:RequestTimeout"] = "00:00:10";
            _settings["Chaos:ShutdownTimeout"] = "00:00:05";

            // Setup on, and every file it writes inside this test's own temp
            // directory. Nothing here touches LocalApplicationData.
            _settings["Chaos:AutoSetup"] = "true";
            _settings["Chaos:SetupLogPath"] = Path.Combine(_stateDirectory, "chaos-setup.log");
            _settings["Chaos:SetupStateFile"] = Path.Combine(_stateDirectory, "chaos-setup-state.json");
            _settings["Chaos:SetupTimeout"] = "00:00:30";
            _settings["Chaos:SetupProbeTimeout"] = "00:00:15";
            _settings["Chaos:SetupCommandTimeout"] = "00:00:20";
        }

        /// <summary>Sets a raw configuration key under the <c>Chaos</c> section.</summary>
        /// <param name="key">Key relative to the section.</param>
        /// <param name="value">The value.</param>
        /// <returns>This builder.</returns>
        public Builder With(string key, string? value)
        {
            _settings[$"Chaos:{key}"] = value;
            return this;
        }

        /// <summary>Turns the automatic run at startup on or off.</summary>
        /// <param name="enabled">Whether it is on.</param>
        /// <returns>This builder.</returns>
        public Builder WithAutoSetup(bool enabled) =>
            With("AutoSetup", enabled ? "true" : "false");

        /// <summary>Points the design-package inspection at a directory.</summary>
        /// <param name="path">The directory.</param>
        /// <returns>This builder.</returns>
        public Builder WithDataDirectory(string path) => With("DataDirectory", path);

        /// <summary>Builds the factory.</summary>
        /// <returns>The factory.</returns>
        public SetupHostFactory Build()
        {
            Directory.CreateDirectory(_stateDirectory);

            // Unless a test says otherwise, this host looks like a machine that
            // has its design package where the platform's layout puts it: data/
            // beside the runtime's working directory. The gateway derives the
            // path from the working directory the runner reports, so this also
            // exercises that derivation rather than short-circuiting it with an
            // explicit Chaos:DataDirectory.
            if (!_settings.ContainsKey("Chaos:DataDirectory"))
            {
                var workingDirectory = Path.Combine(_stateDirectory, "workdir");
                Directory.CreateDirectory(Path.Combine(workingDirectory, "data"));
                File.WriteAllText(
                    Path.Combine(workingDirectory, "data", "asset_register.yaml"),
                    "assets: []\n");
                _runner.WorkingDirectory = workingDirectory;
            }

            return new SetupHostFactory(_settings, _runner, _stateDirectory);
        }
    }
}
