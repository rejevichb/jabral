#!/usr/bin/env python

import argparse, socket, json, select, copy

DEBUG = True
# DEBUG = False

''''
DD_String = a decimal dotted netmask string, i.e. "129.10.0.0"
A DD_String is either a DDAddrString, meaning the string is an address, or a DDNetmaskString, meaning it is a netmask
FTableEntry = an entry in the forwarding table. ie:
{
      "network":    "<network prefix>",           # Example: 12.0.0.0
      "netmask":    "<associated CIDR netmask>",  # Example: 255.0.0.0
      "localpref":  "<integer>",                  # Example: 100
      "selfOrigin": "<true|false>",
      "ASPath":     "{<nid>, [nid], ...}",        # Examples: [1] or [3, 4] or [1, 4, 3]
      "origin":     "<IGP|EGP|UNK>",
    }

'''''

parser = argparse.ArgumentParser(description='route packets')
parser.add_argument('networks', metavar='networks', type=str, nargs='+', help="networks")
args = parser.parse_args()

##########################################################################################

# Message Fields
TYPE = "type"
SRCE = "src"
DEST = "dst"
MESG = "msg"
TABL = "table"

# Message Types
DATA = "data"
DUMP = "dump"
UPDT = "update"
RVKE = "revoke"
NRTE = "no route"

# Update Message Fields
NTWK = "network"
NMSK = "netmask"
ORIG = "origin"
LPRF = "localpref"
APTH = "ASPath"
SORG = "selfOrigin"

# internal route info
CUST = "cust"
PEER = "peer"
PROV = "prov"


##########################################################################################

class Router:
	routes = None
	updates = None
	relations = None
	sockets = None
	fwd_table = None

	def __init__(self, networks):
		self.routes = {}
		self.updates = {}  # Save update annoucements as { src : annoucement_msg }
		self.relations = {}  # {"192.168.0.2" : "peer"}
		self.sockets = {}  # socket.socket() for "192.168.0.2"
		self.fwd_table = []  #

		for relationship in networks:
			# Ex: 192.168.0.2-peer
			network, relation = relationship.split("-")  # network = "192.168.0.2", relation = "peer"
			# if DEBUG:
			print("Starting socket for {} - {}".format(network, relation))
			self.sockets[network] = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
			self.sockets[network].setblocking(0)
			self.sockets[network].connect(network)

			self.relations[network] = relation

		print("\n\n\n\n")
		return

	def lookup_routes(self, daddr):
		""" Lookup all valid routes for an address """
		# !!! DADDR IS A FULL PACKET, NOT JUST ADDRESS !!!
		# Perform DFS for the router at IP address == daddr
		# Return list of lists representing possible paths
		outroutes = []
		curr_max_match = 0
		# Simple count of valid paths by checking forwarding table.
		# For each entry in fwd_table, checks equality of subnets of fwd_table entry and dst
		# for entry in self.fwd_table:
		#	if self.get_subnet(daddr["dst"], entry["netmask"]) == self.get_subnet(entry["network"], entry["netmask"]):
		#		outroutes.append(entry)

		for entry in self.fwd_table:
			num_matches = 0
			split_dst = daddr["dst"].split(".")
			split_entry = entry["network"].split(".")

			for index, octet in enumerate(split_dst):
				if octet == split_entry[index]:
					num_matches += 1
				else:
					break
			print("Compared {} and {}, found {} octet matches".format(daddr["dst"], entry["network"], num_matches))

			if num_matches > curr_max_match:
				curr_max_match = num_matches
				outroutes = [entry]
			elif num_matches == curr_max_match:
				outroutes.append(entry)

		return outroutes

	def get_shortest_as_path(self, routes):
		""" select the route with the shortest AS Path """
		# TODO
		outroutes = []

		curr_shortest_as = None
		for route in routes:
			if curr_shortest_as is None:
				curr_shortest_as = len(route["ASPath"])
			print("Current Shortest AS set to {}".format(len(route["ASPath"])))
			if len(route["ASPath"]) <= curr_shortest_as:
				outroutes.append(route)
			print("{} routes with shortest_path".format(len(outroutes)))
		print(outroutes)
		return outroutes

	def get_highest_preference(self, routes):
		""" select the route with the highest localpref """
		# TODO
		if len(routes) == 0:
			return

		outroutes = []
		curr_max_lp = None

		for route in routes:
			if curr_max_lp is None:
				curr_max_lp = route["localpref"]

			if route["localpref"] >= curr_max_lp:
				curr_max_lp = route["localpref"]
				outroutes.append(route)

		return outroutes

	def get_self_origin(self, routes):
		""" select self originating routes """
		outroutes = []

		if len(routes) <= 1:
			return routes

		if "True" not in [d["selfOrigin"].decode('utf-8') for d in routes]:
			return routes
		else:
			for route in routes:
				if route['selfOrigin'].decode('utf-8') == "True":
					print("Route added: {}".format(route))
					outroutes.append(route)

		return outroutes

	def get_origin_routes(self, routes):
		""" select origin routes: EGP > IGP > UNK """
		# TODO
		outroutes = []

		if len(routes) <= 1:
			return routes

		origin_ranks = {"IGP": 1,
						"EGP": 2,
						"UNK": 3}

		# curr_best_origin = origin_ranks[routes[0]["origin"]]
		curr_best_origin = None
		for route in routes:
			if curr_best_origin is None:
				curr_best_origin = origin_ranks[route["origin"]]

			if origin_ranks[route["origin"]] <= curr_best_origin:
				curr_best_origin = origin_ranks[route["origin"]]
				outroutes.append(route)

		return outroutes

	def filter_relationships(self, srcif, routes):
		""" Don't allow Peer->Peer, Peer->Prov, or Prov->Peer forwards """
		outroutes = []
		if self.relations[srcif] == CUST:
			print("{} SRCIF IS CUST\n".format(srcif))
			return routes
		elif self.relations[srcif] == PEER:
			print("{} SRCIF IS PEER\n".format(srcif))
			for route in routes:
				if self.relations[route["peer"]] == CUST:
					outroutes.append(route)

		else:
			print("SRCIF IS PROV")
			for route in routes:
				if self.relations[route["peer"]] != PEER:
					outroutes.append(route)

		return outroutes[0] if len(outroutes) == 1 else None

	def get_min_ip(self, routes):
		temp = {}

		for route in routes:
			temp[int("".join(route["peer"].split(".")))] = route

		return temp[min(list(temp.keys()))]["peer"]

	def send_no_route(self, srcif, packet):
		self.forward(srcif, {"src": srcif[:-1] + "1",
							 "dst": packet["src"],
							 "type": "no route",
							 "msg": {}})

	def get_route(self, srcif, daddr):
		"""	Select the best route for a given address	"""
		# TODO
		peer = None
		routes = self.lookup_routes(daddr)
		print(routes)
		if len(routes) == 0 or routes is None:
			packet_copy = copy.deepcopy(daddr)
			print("No Routes Found\n")

			# Forwards message back to sender
			print("Sending packet back {}\n".format(packet_copy))
			self.send_no_route(srcif, packet_copy)

			return

		elif len(routes) == 1:
			packet_copy = copy.deepcopy(daddr)
			print("1 route found, forwarding packet to {}".format(packet_copy["dst"]))
			# Forwards message using only available path
			if self.filter_relationships(srcif, routes) is not None:
				return self.forward(routes[0]["peer"], packet_copy)
			else:
				return self.send_no_route(srcif, packet_copy)

		# Rules go here
		else:
			print("{} routes found, finding best route\n".format(len(routes)))
			# 1. Highest Preference
			routes = self.get_highest_preference(routes)
			print("Route list after get_highest_preference: {}\n".format(routes))
			# 2. Self Origin
			routes = self.get_self_origin(routes)
			print("Route list after get_self_origin: {}\n".format(routes))
			# 3. Shortest ASPath
			routes = self.get_shortest_as_path(routes)
			print("Route list after get_shortest_as_path: {}\n".format(routes))
			# 4. EGP > IGP > UNK
			routes = self.get_origin_routes(routes)
			print("Route list after get_origin_routes: {}\n".format(routes))
			# 5. Lowest IP Address
			# TODO assign peer to return of filter_relationships
			# Once it is writter
			# peer = self.get_min_ip(routes)
			routes = self.get_min_ip(routes)
			# Final check: enforce peering relationships
			peer = self.filter_relationships(srcif, routes)

		return self.forward(peer, daddr) if peer is not None else self.send_no_route(srcif, daddr)

	def forward(self, srcif, packet):
		"""	Forward a data packet	"""
		print("Forwarding {} to {}".format(packet["type"], packet["dst"]))

		self.sockets[srcif].sendto(json.dumps(packet), packet["dst"])
		for each in self.fwd_table:
			print(each)
		print("\n")
		return

	# FWTableEntry -> [4]BinString
	def get_nw_prefix_binary(self, entry):
		"""get the network prefix in a list of 4 chunks of 8 bytes"""
		print(entry)
		nw_split = entry["network"].split(".")
		temp = []
		for each in nw_split:
			temp.append(bin(int(each)))

		return temp

	# return list(map(lambda x: format(int(bin(int(x))), "08b"), nw_split))

	# ([4]byte, [4]byte, int) -> bool
	def nw_prefix_coalesce(self, binary_addr1, binary_addr2, netmask_len):
		""" can the entry be aggregated based on length of prefix"""
		matching = 0
		a = ""
		b = ""
		for index, each in enumerate(binary_addr1):
			a += bin(int(each[2:]))[2:]
			b += bin(int(binary_addr2[index][2:]))[2:]
		for i in range(netmask_len - 1):
			if a[i] == b[i]:
				matching += 1
			else:
				return False
		return True

	# DDNetmaskString -> DDNetmaskString
	def decrement_netmask(self, netmask):
		split_nm = netmask.split(".")
		# Get index of final non-zero octet in netmask
		# If all octets are non-zero, return final position
		# as target. Else, make target the index before the first 0
		if "0" not in split_nm:
			target = 3
		else:
			target = split_nm.index("0") - 1

		if split_nm[target] == "255":
			split_nm[target] = "254"
		else:
			split_nm[target][split_nm[target].index("0") - 1] = 0

		print("{} converted to {}".format(netmask, ".".join(split_nm)))
		return ".".join(split_nm)

	# Int -> DDNetmaskString
	def mask(self, i):
		# full_octets = i // 8
		# not_full_octets = i % 8
		# final = ""
		# if full_octets < 4:
		# final += "11111111." * full octets
		# final += "1" * not_full_octets + "0" * 8 - not_full_octets
		# for i in range(full_octets):
		#		final += "1" * 8
		# return ("1" * i)

		if i <= 8:
			return str(int(("1" * i) + ("0" * (8 - i)), 2)) + ".0.0.0"
		elif i <= 16:
			return "255." + str(int(("1" * (i - 8)) + ("0" * (8 - (i - 8))), 2)) + ".0.0"
		elif i <= 24:
			return "255.255." + str(int(("1" * (i - 16)) + ("0" * (8 - (i - 16))), 2)) + ".0"
		else:
			return "255.255.255." + str(int(("1" * (i - 24)) + ("0" * (8 - (i - 24))), 2))

	# (FWTableEntry, FWTableEntry, int) -> FWTableEntry
	def fwt_entry_from_coalesce(self, entry1, entry2, nm_len):
		cp = copy.deepcopy(entry1)
		smallr_ip = self.pick_smaller_ip(entry1["network"], entry2["network"])
		cp["network"] = smallr_ip
		cp["netmask"] = self.mask(nm_len - 1)
		print(cp["netmask"])
		cp["network"] = self.get_subnet(cp["network"], cp["netmask"])
		return cp

	# (string, string) -> string
	def pick_smaller_ip(self, a, b):
		a_split = a.split(".")
		b_split = b.split(".")
		for index, octet in enumerate(a_split):
			if octet > b_split[index]:
				return b
			elif octet < b_split[index]:
				return a

	# -> void
	def coalesce(self):
		"""	coalesce any routes that are right next to each other	"""
		# if we do end up coalescing, we will need to both add and remove routes from table
		old = copy.deepcopy(self.fwd_table)
		to_remove = []
		to_add = []
		found = False
		# can we coalesce?
		for c_entry in self.fwd_table:
			# look at this one entry
			nm = c_entry["netmask"]
			peer = c_entry["peer"]
			prefix = self.get_nw_prefix_binary(c_entry)
			# compare to the rest of the table
			for entry in self.fwd_table:
				# must match exactly on netmask and peer, and must ignore comparing entry to self.
				if entry != c_entry and nm == entry["netmask"] and peer == entry["peer"]:
					# does the prefix allow for aggregation?
					nm_length = "".join(map(lambda x: format(int(x), "08b"), nm.split("."))).count("1")
					if self.nw_prefix_coalesce(prefix, self.get_nw_prefix_binary(entry), nm_length):
						print("Aggregating:\n{}\n{}\n\n".format(c_entry, entry))
						# to_remove.append(c_entry)
						# to_remove.append(entry)  # aggregate the two routes to remove
						to_add.append(
							self.fwt_entry_from_coalesce(c_entry, entry, nm_length))  # calculate the new entry to add
						for each in [entry, c_entry]:
							if each not in to_add:
								to_remove.append(each)
						found = True

						break

		if found:
			print("TO ADD:{}".format(to_add))
			self.fwd_table.extend(to_add)
			temp = []
			for each in self.fwd_table:
				if each not in temp and each not in to_remove:
					temp.append(each)
			self.fwd_table = temp
			print("FINAL: {}".format(self.fwd_table))

			return True

		return False

	# (DDAddressString, Packet) -> void
	def update(self, srcif, packet):
		"""	handle update packets	"""
		# Save update message for later
		self.updates[packet["src"]] = packet

		"""Add an entry to the forwarding table """
		# TODO: Add functionality to prevent duplicate entries
		self.fwd_table.append({"network": packet["msg"]["network"],
							   "netmask": packet["msg"]["netmask"],
							   "localpref": packet["msg"]["localpref"],
							   "selfOrigin": packet["msg"]["selfOrigin"],
							   "ASPath": packet["msg"]["ASPath"],
							   "origin": packet["msg"]["origin"],
							   "peer": packet["src"]})
		print("Forwarding table before agg:\n{}".format(self.fwd_table))

		if len(self.fwd_table) > 1:
			while self.coalesce():
				self.coalesce()

		print("Forwarding Table after agg:\n{}".format(self.fwd_table))

		print("Got update from {}".format(srcif))

		""" Update all neighbors if srcif is CUST
			Update received from a customer: send updates to all other neighbors """
		if self.relations[srcif] == CUST:
			for sock in self.sockets:
				# Prevent self-sending
				if sock != packet["src"]:
					packet_copy = copy.deepcopy(packet)

					"""Update is received at the router and passed to other sockets
						so old dst becomes new src and sock becomes new dst """
					packet_copy["src"], packet_copy["dst"] = packet_copy["dst"], sock

					self.forward(sock, packet_copy)
		else:
			for sock in self.sockets:
				# Prevent self-sending
				if sock != packet["src"] and self.relations[sock] == CUST:
					packet_copy = copy.deepcopy(packet)

					""" Update is received at the router and passed to other sockets
						so old dst becomes new src and sock becomes new dst"""
					packet_copy["src"], packet_copy["dst"] = packet_copy["dst"], sock

					self.forward(sock, packet_copy)
		return

	# (DDAddressString, Packet) -> bool
	def revoke(self, srcif, packet):
		"""	handle revoke packets	"""
		rev_msg = packet["msg"]  # packet message contents
		temp = []

		for unreachable in rev_msg:
			for entry in self.fwd_table:
				if entry["network"] == unreachable["network"] and entry["netmask"] == unreachable["netmask"] and entry[
					"peer"] == packet["src"]:
					print("{} revoked".format(unreachable["network"]))
					continue
				else:
					if entry not in temp:
						temp.append(entry)
			self.fwd_table = temp

		if self.relations[srcif] == CUST:
			for sock in self.sockets:
				if sock != packet["src"]:
					packet_copy = copy.deepcopy(packet)

					""" Update is received at the router and passed to other sockets
						so old dst becomes new src and sock becomes new dst """
					packet_copy["src"], packet_copy["dst"] = packet_copy["dst"], sock

					self.forward(sock, packet_copy)
		else:
			for sock in self.sockets:
				if sock != packet["src"] and self.relations[sock] == CUST:
					packet_copy = copy.deepcopy(packet)
					packet_copy["src"], packet_copy["dst"] = packet_copy["dst"], sock

					self.forward(sock, packet_copy)

		# TODO
		return True

	# Packet -> void
	def dump(self, packet):
		"""	handles dump table requests	"""
		print(packet)

		""" Forward the current forwarding table for comparison.
			Filters socket list to exclude socket that sent the packet then takes first of list
			self.forward([s for s in self.sockets if s != packet["src"]][0],"""

		self.forward(packet["src"],
					 {"src": packet["dst"],
					  "dst": packet["src"],
					  "type": "table",
					  "msg": self.fwd_table})

	# Packet -> bool
	def handle_packet(self, srcif, packet):
		"""Switch over packet type and choose which action to take"""
		if packet["type"] == UPDT:
			print("Handling update")
			self.update(srcif, packet)
		if packet["type"] == DATA:
			print("Checking for possible routes data message")
			self.get_route(srcif, packet)
		if packet["type"] == DUMP:
			print("Dumping forwarding table")
			self.dump(packet)
		if packet["type"] == RVKE:
			print("Revoking entry in forwarding table")
			self.revoke(srcif, packet)
		if packet["type"] == NRTE:
			print("No route for packet")
			self.send_no_route(srcif, packet)
		if packet["type"] == "wait":
			print("wait")
			pass

		return False

	# (Connection, Msg) -> void
	def send_error(self, conn, msg):
		pass

	# TODO not used
	# DDAddrString, DDNetmaskString
	def get_subnet(self, ip, netmask):
		"""returns subnet of IP based on netmask"""
		split_ip = ip.split(".")
		split_nm = netmask.split(".")
		temp = []

		# For each chunk of IP, bitwise-AND with netmask and append to temp
		for index, each in enumerate(split_ip):
			temp.append(str(int(each) & int(split_nm[index])))

		return ".".join(temp)

	def run(self):
		while True:
			socks = select.select(self.sockets.values(), [], [], 0.1)[0]
			for conn in socks:
				try:
					k = conn.recv(65535)
				except:
					# either died on a connection reset, or was SIGTERM's by parent
					return
				if k:
					for sock in self.sockets:
						if self.sockets[sock] == conn:
							srcif = sock
							msg = json.loads(k)
					if not self.handle_packet(srcif, msg):
						self.send_error(conn, msg)
				else:
					return

		return


if __name__ == "__main__":
	router = Router(args.networks)
	router.run()