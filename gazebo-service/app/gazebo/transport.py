"""Transport 13 Node with blocking native calls that release the Python GIL."""
from gz.transport13 import Node as GazeboNode
from .. import _transport_request


class Node(GazeboNode):
    def request(self, service, request, request_type, response_type, timeout):
        ok, data = _transport_request.request_raw(self, service, request.SerializeToString(),
            request_type.DESCRIPTOR.full_name, response_type.DESCRIPTOR.full_name, timeout)
        response = response_type()
        if data:
            response.ParseFromString(data)
        return ok, response

    def unsubscribe(self, topic):
        return _transport_request.unsubscribe(self, topic)
