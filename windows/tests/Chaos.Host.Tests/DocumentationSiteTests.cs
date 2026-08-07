using System.Net;
using Chaos.Host.Docs;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>
/// The documentation listener: what it serves, what it refuses, and what it
/// says when there is nothing to serve.
/// </summary>
/// <remarks>
/// Every test here runs the real pipeline on 127.0.0.1 with an ephemeral port.
/// No fixed port, no network, no Python, no PowerShell.
/// </remarks>
public sealed class DocumentationSiteTests
{
    [Fact]
    public async Task Serves_the_generated_site_when_it_is_present()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        using var index = await server.Client.GetAsync("/");
        Assert.Equal(HttpStatusCode.OK, index.StatusCode);
        Assert.Contains(DocumentationFixture.IndexMarker, await index.Content.ReadAsStringAsync(), StringComparison.Ordinal);
        Assert.Equal("text/html", index.Content.Headers.ContentType?.MediaType);

        using var page = await server.Client.GetAsync("/alarms.html");
        Assert.Equal(HttpStatusCode.OK, page.StatusCode);
        Assert.Contains(DocumentationFixture.PageMarker, await page.Content.ReadAsStringAsync(), StringComparison.Ordinal);

        using var asset = await server.Client.GetAsync("/assets/help.css");
        Assert.Equal(HttpStatusCode.OK, asset.StatusCode);
        Assert.Equal("text/css", asset.Content.Headers.ContentType?.MediaType);
    }

    [Fact]
    public async Task Serves_the_single_file_build_that_ships_beside_the_directory_site()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        // The generator emits one self-contained file as well as the site. It is
        // the copy an operator saves to a phone before a storm, so it has to be
        // reachable, not just present on disk.
        using var response = await server.Client.GetAsync("/chaos-help-offline.html");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Contains(
            DocumentationFixture.SingleFileMarker,
            await response.Content.ReadAsStringAsync(),
            StringComparison.Ordinal);
    }

    [Fact]
    public async Task Revalidates_documentation_pages_so_an_upgrade_is_not_missed()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        using var response = await server.Client.GetAsync("/index.html");

        var cacheControl = response.Headers.CacheControl;
        Assert.NotNull(cacheControl);
        Assert.True(cacheControl!.NoCache, "Documentation pages must be revalidated; a wall display must not show last month's procedure.");
    }

    [Fact]
    public async Task Explains_how_to_build_it_from_visual_studio_when_the_site_is_absent()
    {
        await using var server = await DocumentationServer.StartAsync(DocumentationFixture.AbsentResolution());

        using var response = await server.Client.GetAsync("/");
        var body = await response.Content.ReadAsStringAsync();

        // 200, not 404: nothing is broken and no URL needs correcting.
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal(DocumentationPages.StateNotGenerated, response.Headers.GetValues(DocumentationPages.StateHeader).Single());

        // It has to say what to do, in the IDE, without a terminal.
        Assert.Contains("Chaos.Runtime", body, StringComparison.Ordinal);
        Assert.Contains("Solution Explorer", body, StringComparison.Ordinal);
        Assert.Contains("Build", body, StringComparison.Ordinal);

        // And where it looked, so the answer is diagnosable rather than a guess.
        foreach (var searched in DocumentationFixture.AbsentResolution().SearchedPaths)
        {
            Assert.Contains(searched, body, StringComparison.Ordinal);
        }

        // Never an exception page.
        Assert.DoesNotContain("Exception", body, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("at Microsoft.AspNetCore", body, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Explains_itself_on_every_path_when_the_site_is_absent()
    {
        await using var server = await DocumentationServer.StartAsync(DocumentationFixture.AbsentResolution());

        foreach (var path in new[] { "/", "/index.html", "/alarms.html", "/deep/nested/page.html" })
        {
            using var response = await server.Client.GetAsync(path);

            Assert.Equal(HttpStatusCode.OK, response.StatusCode);
            Assert.Contains(
                "has not been built yet",
                await response.Content.ReadAsStringAsync(),
                StringComparison.OrdinalIgnoreCase);
        }
    }

    [Fact]
    public async Task An_unknown_page_of_a_generated_site_is_explained_rather_than_thrown()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        using var response = await server.Client.GetAsync("/no-such-page.html");
        var body = await response.Content.ReadAsStringAsync();

        // The site exists; this page does not. 404 is the honest answer, and it
        // is still a page rather than a stack trace.
        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
        Assert.Equal(DocumentationPages.StateNotFound, response.Headers.GetValues(DocumentationPages.StateHeader).Single());
        Assert.Equal("text/html", response.Content.Headers.ContentType?.MediaType);
        Assert.DoesNotContain("at Microsoft.AspNetCore", body, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Exposes_no_api_and_no_proxy()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        // Everything the gateway serves on its own port, asked for here. None of
        // it exists on this listener: no route table, no forwarder, no setup
        // endpoints were ever registered in this application.
        foreach (var path in new[]
                 {
                     "/api/v1/alarms/active",
                     "/api/v1/registry/assets",
                     "/health",
                     "/host/info",
                     "/host/routes",
                     "/host/setup",
                     "/ui/annunciator.html",
                 })
        {
            using var response = await server.Client.GetAsync(path);

            Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);

            // A proxied response carries the gateway's ownership headers. Nothing
            // here was proxied anywhere.
            Assert.False(
                response.Headers.Contains("X-Chaos-Route-Owner"),
                $"{path} looks like it was routed by the gateway's manifest.");
            Assert.Equal("text/html", response.Content.Headers.ContentType?.MediaType);
        }
    }

    [Fact]
    public async Task Refuses_every_method_that_could_change_anything()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        foreach (var method in new[] { HttpMethod.Post, HttpMethod.Put, HttpMethod.Delete, HttpMethod.Patch })
        {
            using var request = new HttpRequestMessage(method, "/host/setup/run");
            using var response = await server.Client.SendAsync(request);

            Assert.Equal(HttpStatusCode.MethodNotAllowed, response.StatusCode);
            Assert.Contains("GET", response.Content.Headers.Allow);
            Assert.Contains("HEAD", response.Content.Headers.Allow);
            Assert.DoesNotContain(method.Method, response.Content.Headers.Allow);
        }

        // The read-only guard applies to the site itself too, not only to paths
        // that happen not to exist.
        using var writeToAPage = new HttpRequestMessage(HttpMethod.Post, "/index.html");
        using var refused = await server.Client.SendAsync(writeToAPage);
        Assert.Equal(HttpStatusCode.MethodNotAllowed, refused.StatusCode);
    }

    [Fact]
    public async Task Answers_head_the_same_way_it_answers_get()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        using var request = new HttpRequestMessage(HttpMethod.Head, "/index.html");
        using var response = await server.Client.SendAsync(request);

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public async Task Serves_nothing_outside_the_documentation_directory()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        foreach (var path in new[] { "/../appsettings.json", "/..%2fappsettings.json", "/%2e%2e/appsettings.json" })
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, server.Url.TrimEnd('/') + path);
            using var response = await server.Client.SendAsync(request);

            Assert.NotEqual(HttpStatusCode.OK, response.StatusCode);
        }
    }

    [Fact]
    public async Task Reports_the_port_it_actually_bound()
    {
        using var fixture = DocumentationFixture.CreateSite();
        await using var server = await DocumentationServer.StartAsync(fixture.Resolution());

        // Port 0 means "whatever is free", so the bound address is the only
        // place the real port appears - which is what /host/info reports.
        Assert.StartsWith("http://127.0.0.1:", server.Url, StringComparison.Ordinal);
        Assert.DoesNotContain(":0", server.Url, StringComparison.Ordinal);
    }
}
