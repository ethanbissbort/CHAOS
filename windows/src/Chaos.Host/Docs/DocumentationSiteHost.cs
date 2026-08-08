using Chaos.Host.Configuration;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Docs;

/// <summary>
/// Runs the documentation site alongside the gateway, on its own port.
/// </summary>
/// <remarks>
/// <para>
/// One process, two listeners. The documentation site is a separate
/// <see cref="WebApplication"/> (see <see cref="DocumentationSite"/> for why it
/// is separate rather than a second endpoint on the gateway) and this hosted
/// service is what ties its lifetime to the gateway's.
/// </para>
/// <para>
/// <b>It can never take the gateway down.</b> A failure to bind — the port is
/// taken, an ACL forbids it, the address is nonsense — is logged and recorded
/// for <c>/host/info</c>, and start returns normally. Losing the manual is
/// annoying; losing the control plane on an off-grid site because the manual
/// could not bind a port would be indefensible.
/// </para>
/// </remarks>
internal sealed partial class DocumentationSiteHost : IHostedService, IAsyncDisposable
{
    /// <summary>
    /// The in-memory test server, by name.
    /// </summary>
    /// <remarks>
    /// Under <c>WebApplicationFactory</c> the gateway's <see cref="IServer"/> is
    /// this type: the whole host runs without a socket layer. Starting a real
    /// Kestrel for the documentation there would mean a unit test binding a LAN
    /// port as a side effect of building a host — a hidden dependency this
    /// codebase refuses everywhere else. Tests that want the documentation site
    /// build it directly through <see cref="DocumentationSite.Create"/>, which
    /// is the real pipeline, on an ephemeral loopback port.
    /// </remarks>
    private const string TestServerTypeName = "Microsoft.AspNetCore.TestHost.TestServer";

    private readonly DocumentationOptions _options;
    private readonly DocumentationSiteState _state;
    private readonly IHostEnvironment _environment;
    private readonly IServer _gatewayServer;
    private readonly ILogger<DocumentationSiteHost> _logger;

    private WebApplication? _site;

    public DocumentationSiteHost(
        DocumentationSiteState state,
        IHostEnvironment environment,
        IServer gatewayServer,
        ILogger<DocumentationSiteHost> logger)
    {
        // Deliberately the settings the state was built from, rather than a
        // second IOptions resolution: the URL /host/info reports and the URL
        // this binds are then provably the same string.
        _options = state.Options;
        _state = state;
        _environment = environment;
        _gatewayServer = gatewayServer;
        _logger = logger;
    }

    public async Task StartAsync(CancellationToken cancellationToken)
    {
        if (!_options.Enabled)
        {
            _state.MarkNotStarted(
                "Switched off by configuration (Chaos:Docs:Enabled=false). The gateway is unaffected.");
            LogDisabled();
            return;
        }

        var serverType = _gatewayServer.GetType().FullName;
        if (serverType == TestServerTypeName)
        {
            _state.MarkNotStarted(
                "Not started: this gateway is running on an in-memory test server, which binds no sockets.");
            LogNoSockets(serverType);
            return;
        }

        try
        {
            var site = DocumentationSite.Create(_state.Root, _options.ListenUrl, _environment.ContentRootPath);
            _site = site;
            await site.StartAsync(cancellationToken).ConfigureAwait(false);

            var bound = DocumentationSite.BoundAddresses(site);
            var reported = bound.Count > 0 ? string.Join(';', bound) : _options.ListenUrl;
            _state.MarkListening(reported);

            if (_state.Root.HasIndex)
            {
                LogListening(reported, _state.Root.Path!);
            }
            else
            {
                LogListeningWithoutContent(reported);
            }
        }
        catch (Exception ex)
        {
            // Deliberately broad: whatever went wrong with the documentation, the
            // gateway continues. The reason is preserved and reported.
            _state.MarkFailed(ex.Message);
            LogStartFailed(_options.ListenUrl, ex);
            await DisposeSiteAsync().ConfigureAwait(false);
        }
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        if (_site is null)
        {
            return;
        }

        try
        {
            await _site.StopAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            LogStopFailed(ex);
        }
        finally
        {
            await DisposeSiteAsync().ConfigureAwait(false);
        }
    }

    public async ValueTask DisposeAsync() => await DisposeSiteAsync().ConfigureAwait(false);

    private async Task DisposeSiteAsync()
    {
        if (_site is null)
        {
            return;
        }

        var site = _site;
        _site = null;

        try
        {
            await site.DisposeAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            LogStopFailed(ex);
        }
    }

    [LoggerMessage(EventId = 3401, Level = LogLevel.Information,
        Message = "The documentation listener is switched off (Chaos:Docs:Enabled=false).")]
    private partial void LogDisabled();

    [LoggerMessage(EventId = 3402, Level = LogLevel.Debug,
        Message = "The documentation listener was not started: the gateway is hosted on {ServerType}, which binds "
                + "no sockets.")]
    private partial void LogNoSockets(string? serverType);

    [LoggerMessage(EventId = 3403, Level = LogLevel.Information,
        Message = "Documentation site listening on {ListenUrl}, serving {Path}. It has no API, no proxy and no "
                + "write endpoints.")]
    private partial void LogListening(string listenUrl, string path);

    [LoggerMessage(EventId = 3404, Level = LogLevel.Information,
        Message = "Documentation site listening on {ListenUrl}, but no site has been generated yet. Every page "
                + "explains how to build it from Visual Studio.")]
    private partial void LogListeningWithoutContent(string listenUrl);

    [LoggerMessage(EventId = 3405, Level = LogLevel.Error,
        Message = "The documentation listener could not start on {ListenUrl}. The gateway is unaffected and "
                + "continues to serve the operator console and the platform API.")]
    private partial void LogStartFailed(string listenUrl, Exception exception);

    [LoggerMessage(EventId = 3406, Level = LogLevel.Warning,
        Message = "The documentation listener did not stop cleanly.")]
    private partial void LogStopFailed(Exception exception);
}
