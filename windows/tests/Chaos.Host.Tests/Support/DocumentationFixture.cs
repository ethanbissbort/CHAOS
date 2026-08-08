using Chaos.Host.Docs;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Tests.Support;

/// <summary>
/// A generated documentation site on disk, in a temporary directory.
/// </summary>
/// <remarks>
/// The real generator (<c>tools/build_docs.py</c>) is not run: these tests are
/// hermetic and must not need Python, a network or a checkout in any particular
/// state. What is under test is the listener, so the fixture writes the shapes
/// the generator produces — an <c>index.html</c>, further pages, an asset, and
/// the single-file build beside them — and nothing else.
/// </remarks>
internal sealed class DocumentationFixture : IDisposable
{
    /// <summary>Marker string inside the fixture's index page.</summary>
    public const string IndexMarker = "FIXTURE-INDEX-PAGE";

    /// <summary>Marker string inside the fixture's second page.</summary>
    public const string PageMarker = "FIXTURE-ALARMS-PAGE";

    /// <summary>Marker string inside the fixture's single-file build.</summary>
    public const string SingleFileMarker = "FIXTURE-SINGLE-FILE";

    private DocumentationFixture(string root) => Root = root;

    /// <summary>The directory the site was written to.</summary>
    public string Root { get; }

    /// <summary>Writes a small but complete site and returns the fixture.</summary>
    /// <returns>The fixture; dispose it to delete the directory.</returns>
    public static DocumentationFixture CreateSite()
    {
        var root = Path.Combine(Path.GetTempPath(), "chaos-docs-" + Guid.NewGuid().ToString("n"));
        Directory.CreateDirectory(Path.Combine(root, "assets"));

        File.WriteAllText(
            Path.Combine(root, "index.html"),
            $"<!doctype html><title>CHAOS help</title><h1>{IndexMarker}</h1>");
        File.WriteAllText(
            Path.Combine(root, "alarms.html"),
            $"<!doctype html><title>Alarms</title><h1>{PageMarker}</h1>");
        File.WriteAllText(
            Path.Combine(root, "chaos-help-offline.html"),
            $"<!doctype html><title>CHAOS help, one file</title><h1>{SingleFileMarker}</h1>");
        File.WriteAllText(Path.Combine(root, "assets", "help.css"), "body { color: #111; }");

        return new DocumentationFixture(root);
    }

    /// <summary>The resolution a gateway would produce for this fixture.</summary>
    /// <returns>A resolution pointing at <see cref="Root"/>.</returns>
    public DocumentationRootResolution Resolution() => new(Root, "configured", [Root]);

    /// <summary>
    /// The resolution a gateway produces when nothing has been generated.
    /// </summary>
    /// <returns>A resolution with no path and two probed candidates.</returns>
    public static DocumentationRootResolution AbsentResolution() => new(
        null,
        "not-found",
        [
            Path.Combine(Path.GetTempPath(), "chaos-docs-absent", "web", "docs"),
            Path.Combine(Path.GetTempPath(), "chaos-docs-absent", "src", "chaos", "web", "docs"),
        ]);

    public void Dispose()
    {
        try
        {
            if (Directory.Exists(Root))
            {
                Directory.Delete(Root, recursive: true);
            }
        }
        catch (IOException)
        {
            // A temp directory that will not delete is not a test failure.
        }
    }
}

/// <summary>
/// The documentation listener, running for real on an ephemeral loopback port.
/// </summary>
/// <remarks>
/// <para>
/// The real <see cref="DocumentationSite"/> pipeline over a real socket, the
/// same way <see cref="StubBackend"/> does it: port 0, so nothing in the suite
/// assumes a port is free and no test can collide with another or with a
/// gateway someone has running.
/// </para>
/// <para>
/// It is started directly rather than through the gateway on purpose. That is
/// exactly how it runs in the product — a separate application with its own
/// pipeline — and it means these tests exercise the listener without a test
/// ever binding the LAN documentation port.
/// </para>
/// </remarks>
internal sealed class DocumentationServer : IAsyncDisposable
{
    private readonly WebApplication _app;

    private DocumentationServer(WebApplication app, string url)
    {
        _app = app;
        Url = url;
        Client = new HttpClient { BaseAddress = new Uri(url) };
    }

    /// <summary>The address the listener actually bound.</summary>
    public string Url { get; }

    /// <summary>A client pointed at <see cref="Url"/>.</summary>
    public HttpClient Client { get; }

    /// <summary>Starts the documentation listener over the given site.</summary>
    /// <param name="root">Where the site is, or the resolution saying it is not there.</param>
    /// <returns>The started server.</returns>
    public static async Task<DocumentationServer> StartAsync(DocumentationRootResolution root)
    {
        var app = DocumentationSite.Create(
            root,
            "http://127.0.0.1:0",
            AppContext.BaseDirectory,
            builder => builder.Logging.ClearProviders());

        await app.StartAsync();

        var url = DocumentationSite.BoundAddresses(app).FirstOrDefault()
            ?? throw new InvalidOperationException("The documentation listener did not report a bound address.");

        return new DocumentationServer(app, url);
    }

    public async ValueTask DisposeAsync()
    {
        Client.Dispose();
        await _app.StopAsync();
        await _app.DisposeAsync();
    }
}
