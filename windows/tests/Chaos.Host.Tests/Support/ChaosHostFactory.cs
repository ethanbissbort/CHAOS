using Chaos.Host.Routing;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;

namespace Chaos.Host.Tests.Support;

/// <summary>
/// Hosts the real <c>Chaos.Host</c> pipeline in process, with settings supplied
/// per test.
/// </summary>
/// <remarks>
/// Deliberately the real <c>Program</c> rather than a reconstruction: the
/// startup ordering — manifest validation before anything else — is part of what
/// is under test, and a hand-built pipeline would not exercise it.
/// </remarks>
internal sealed class ChaosHostFactory : WebApplicationFactory<Program>
{
    private readonly Dictionary<string, string?> _settings;

    private ChaosHostFactory(Dictionary<string, string?> settings) => _settings = settings;

    /// <summary>Starts a builder for a host whose backend is the given stub.</summary>
    /// <param name="backend">The stub backend, or null for a host with no reachable backend.</param>
    /// <returns>A builder.</returns>
    public static ChaosHostFactoryBuilder For(StubBackend? backend = null) => new(backend?.Url);

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        foreach (var (key, value) in _settings)
        {
            builder.UseSetting(key, value);
        }
    }

    /// <summary>Fluent configuration for <see cref="ChaosHostFactory"/>.</summary>
    internal sealed class ChaosHostFactoryBuilder
    {
        private readonly Dictionary<string, string?> _settings = new(StringComparer.OrdinalIgnoreCase);
        private int _routeIndex;

        internal ChaosHostFactoryBuilder(string? backendUrl)
        {
            // Sane, fast, hermetic defaults for every test host.
            _settings["Chaos:BackendUrl"] = backendUrl ?? StubBackend.ReserveClosedLoopbackUrl();

            // The gateway binds nothing in tests (TestServer replaces Kestrel),
            // but the setting is validated, so keep it legal and harmless.
            _settings["Chaos:ListenUrl"] = "http://127.0.0.1:0";

            // No console assets unless a test asks for them; an empty value would
            // trigger the probe and pick up whatever happens to be on disk.
            _settings["Chaos:WebRootPath"] = Path.Combine(Path.GetTempPath(), "chaos-host-tests-absent-webroot");

            // First-run setup off unless a test asks for it. On, it would launch
            // the platform CLI as a real child process and create a real
            // database - the opposite of hermetic. Setup has its own factory
            // (SetupHostFactory) that injects a fake command runner instead.
            _settings["Chaos:AutoSetup"] = "false";
            // Never launch a real interpreter from a test host.
            _settings["Chaos:SuperviseBackend"] = "false";

            // Keep the "starting" grace window short so backend-down states are
            // reached immediately instead of after a minute.
            _settings["Chaos:BackendStartTimeout"] = "00:00:00.100";
            _settings["Chaos:BackendHealthInterval"] = "00:00:01";
            _settings["Chaos:BackendHealthTimeout"] = "00:00:02";
            _settings["Chaos:BackendHealthMaxBackoff"] = "00:00:01";
            _settings["Chaos:RequestTimeout"] = "00:00:10";
            _settings["Chaos:ShutdownTimeout"] = "00:00:05";
        }

        /// <summary>Sets a raw configuration key under the <c>Chaos</c> section.</summary>
        /// <param name="key">Key relative to the section, for example <c>WebRootPath</c>.</param>
        /// <param name="value">The value.</param>
        /// <returns>This builder.</returns>
        public ChaosHostFactoryBuilder With(string key, string? value)
        {
            _settings[$"Chaos:{key}"] = value;
            return this;
        }

        /// <summary>Adds a route-ownership override.</summary>
        /// <param name="pathPrefix">The prefix.</param>
        /// <param name="owner">Who owns it.</param>
        /// <param name="portedInVersion">Optional ported-in version.</param>
        /// <param name="notes">Optional notes.</param>
        /// <returns>This builder.</returns>
        public ChaosHostFactoryBuilder WithRoute(
            string pathPrefix,
            RouteOwner owner,
            string? portedInVersion = null,
            string? notes = null)
        {
            var index = _routeIndex++;
            _settings[$"Chaos:Routes:{index}:PathPrefix"] = pathPrefix;
            _settings[$"Chaos:Routes:{index}:Owner"] = owner.ToString();
            if (portedInVersion is not null)
            {
                _settings[$"Chaos:Routes:{index}:PortedInVersion"] = portedInVersion;
            }

            if (notes is not null)
            {
                _settings[$"Chaos:Routes:{index}:Notes"] = notes;
            }

            return this;
        }

        /// <summary>Points the operator console at a directory.</summary>
        /// <param name="path">The directory, or null to leave it unset (probing).</param>
        /// <returns>This builder.</returns>
        public ChaosHostFactoryBuilder WithWebRoot(string? path) => With("WebRootPath", path);

        /// <summary>Builds the factory.</summary>
        /// <returns>The factory.</returns>
        public ChaosHostFactory Build() => new(_settings);
    }
}
