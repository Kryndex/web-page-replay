"""Extends BaseHTTPRequestHandler with SSL certificate generation."""
import logging
import socket

import certutils
import httparchive


def get_crt_request(host):
  return httparchive.ArchivedHttpRequest('DUMMY_CERT', host, '', None, {})


class SSLHandshakeHandler:
  """Handles Server Name Indication (SNI) using dummy certs."""

  def setup(self):
    """Sets up connection providing the certificate to the client."""
    self.server_name = None
    # One of: One of SSLv2_METHOD, SSLv3_METHOD, SSLv23_METHOD, or TLSv1_METHOD
    context = certutils.get_ssl_context()
    def handle_servername(connection):
      """A SNI callback that happens during do_handshake()."""
      try:
        host = connection.get_servername()
        if host:
          crt_request = get_crt_request(host)
          crt_response = self.server.get_certificate(crt_request)
          crt = crt_response.response_data[0]
          self.server_name = host
          new_context = certutils.get_ssl_context()
          cert = certutils.load_cert(crt)
          new_context.use_certificate(cert)
          new_context.use_privatekey_file(self.server.pem_path)
          connection.set_context(new_context)
          return new_context
        # else: fail with 'no shared cipher'
      except Exception, e:
        # Do not leak any exceptions or else openssl crashes.
        print('Exception in SNI handler', e)

    context.set_tlsext_servername_callback(handle_servername)
    self.connection = certutils.get_ssl_connection(context, self.connection)
    self.connection.set_accept_state()
    try:
      self.connection.do_handshake()
    except certutils.Error, v:
      host = self.connection.get_servername()
      if not host:
        logging.error('Dropping request without SNI')
        return ''
      raise certutils.Error('SSL handshake error %s: %s' % (host, str(v)))

    def wrap_recv(recv):
      """Wraps recv to handle ragged EOFs and ZeroReturnErrors."""
      def wrapped_recv(buflen=1024, flags=0):
        try:
          return recv(buflen, flags)
        except certutils.SysCallError, e:
          if e.args[1] == 'Unexpected EOF':
            return ''
          raise
        except certutils.ZeroReturnError:
          return ''
      return wrapped_recv
    self.connection.recv = wrap_recv(self.connection.recv)

    # Re-wrap the read/write streams with our new connection.
    self.rfile = socket._fileobject(self.connection, 'rb', self.rbufsize,
                                    close=False)
    self.wfile = socket._fileobject(self.connection, 'wb', self.wbufsize,
                                    close=False)

  def finish(self):
    self.connection.shutdown()
    self.connection.close()


def wrap_handler(handler_class):
  """Wraps a BaseHTTPHandler wtih SSL MITM certificates."""
  if certutils.openssl_import_error:
    raise certutils.openssl_import_error

  class WrappedHandler(SSLHandshakeHandler, handler_class):

    def setup(self):
      handler_class.setup(self)
      SSLHandshakeHandler.setup(self)

    def finish(self):
      handler_class.finish(self)
      SSLHandshakeHandler.finish(self)
  return WrappedHandler
