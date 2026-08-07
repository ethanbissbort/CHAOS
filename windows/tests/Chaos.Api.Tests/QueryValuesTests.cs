using Chaos.Api.Http;

namespace Chaos.Api.Tests;

/// <summary>
/// Boolean query parsing, pinned to what FastAPI accepts and rejects.
/// </summary>
/// <remarks>
/// The accepted set was taken by observation from the running Python endpoint.
/// ASP.NET Core's own binder accepts only "true"/"false", so a caller sending
/// <c>?include_pending=1</c> — which the platform has always accepted — would
/// start failing purely because the route moved to .NET. That is the class of
/// regression a gateway migration produces if nobody looks.
/// </remarks>
public sealed class QueryValuesTests
{
    [Theory]
    [InlineData("true")]
    [InlineData("True")]
    [InlineData("TRUE")]
    [InlineData("1")]
    [InlineData("yes")]
    [InlineData("YES")]
    [InlineData("on")]
    [InlineData("ON")]
    [InlineData("t")]
    [InlineData("y")]
    public void PydanticTruthyValuesParseAsTrue(string raw)
    {
        Assert.True(QueryValues.TryParseBool(raw, defaultValue: false, out var value));
        Assert.True(value);
    }

    [Theory]
    [InlineData("false")]
    [InlineData("False")]
    [InlineData("FALSE")]
    [InlineData("0")]
    [InlineData("no")]
    [InlineData("off")]
    [InlineData("Off")]
    [InlineData("f")]
    [InlineData("n")]
    public void PydanticFalsyValuesParseAsFalse(string raw)
    {
        Assert.True(QueryValues.TryParseBool(raw, defaultValue: true, out var value));
        Assert.False(value);
    }

    [Theory]
    [InlineData("")]
    [InlineData(" ")]
    [InlineData(" true")]
    [InlineData("true ")]
    [InlineData("2")]
    [InlineData("-1")]
    [InlineData("banana")]
    public void EverythingElseIsAValidationFailure(string raw) =>
        // Including the whitespace-padded forms: pydantic does not trim, and a
        // 200 where the platform returns 422 is still a behavioural difference.
        Assert.False(QueryValues.TryParseBool(raw, defaultValue: false, out _));

    [Fact]
    public void AnAbsentParameterTakesTheDefault()
    {
        Assert.True(QueryValues.TryParseBool(null, defaultValue: true, out var value));
        Assert.True(value);
    }

    [Fact]
    public void TheValidationBodyMatchesPydanticsShape()
    {
        var body = ValidationErrorResponse.BoolParsing("include_pending", "banana");

        var detail = Assert.Single(body.Detail);
        Assert.Equal("bool_parsing", detail.Type);
        Assert.Equal(["query", "include_pending"], detail.Loc);
        Assert.Equal("Input should be a valid boolean, unable to interpret input", detail.Msg);
        Assert.Equal("banana", detail.Input);
    }
}
