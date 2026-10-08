from unittest.mock import patch

import pytest

from everyframe_validator.feed import fetch,FeedUnavailable,MAX_BYTES


class Response:
    def __init__(self,status=200,body=b"{}",headers=None):
        self.status=status
        self.body=body
        self.headers=headers or {}

    def getheader(self,name,default=None):return self.headers.get(name,default)

    def read1(self,size):
        part=self.body[:size];self.body=self.body[size:];return part


def download(response):
    with patch("socket.getaddrinfo",return_value=[(0,0,0,"",("8.8.8.8",443))]), \
         patch("socket.create_connection") as socket_call, \
         patch("ssl.create_default_context") as context, \
         patch("http.client.HTTPSConnection") as connection:
        connection.return_value.getresponse.return_value=response
        result=fetch("https://example.com/epochs/",10)
        socket_call.assert_called_once_with(("8.8.8.8",443),timeout=10)
        assert context.return_value.wrap_socket.call_args.kwargs["server_hostname"]=="example.com"
        assert connection.return_value.request.call_args.args[:2]==("GET","/epochs/10.json")
        return result


def test_pinned_ip_preserves_hostname():assert download(Response())==b"{}"


@pytest.mark.parametrize("status",[301,302,307,308,401,500])
def test_redirects_and_errors_rejected(status):
    with pytest.raises(ValueError):download(Response(status))


def test_404_is_waiting():
    with pytest.raises(FeedUnavailable):download(Response(404))


@pytest.mark.parametrize("response",[
    Response(headers={"Content-Length":str(MAX_BYTES+1)}),
    Response(body=b"x"*(MAX_BYTES+1)),
    Response(headers={"Content-Encoding":"gzip"}),
])
def test_size_and_encoding_bounds(response):
    with pytest.raises(ValueError):download(response)
