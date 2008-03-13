/*
 * Copyright (C) 2005, 2006, 2007, 2008 Thomas Cort <tom@tomcort.com>
 *
 * This file is part of libtinypoker.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 */

#include "../main/tinypoker.h"
#include "test.h"

int main()
{
	assertTrue("Busted String should be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED BAGELBOT"));
	assertTrue("Busted String should be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED Coat_Hanger"));
	assertTrue("Busted String should be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED Alpha"));
	assertTrue("Busted String should be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED Beta"));
	assertTrue("Busted String should be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED Gamma"));

	assertFalse("Busted String should not be valid", ipp_validate_msg(REGEX_MSG_BUSTED, ""));
	assertFalse("Busted String should not be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED "));
	assertFalse("Busted String should not be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED\n"));
	assertFalse("Busted String should not be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED \n"));
	assertFalse("Busted String should not be valid", ipp_validate_msg(REGEX_MSG_BUSTED, "BUSTED X 1 1"));
	return PASS;
}